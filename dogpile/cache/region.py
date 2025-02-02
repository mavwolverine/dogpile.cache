from __future__ import annotations

import contextlib
import datetime
from functools import partial
from functools import wraps
import inspect
import json
import logging
from numbers import Number
import threading
import time
from typing import Any
from typing import Callable
from typing import cast
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Type
from typing import TYPE_CHECKING
from typing import Union

from decorator import decorate

from . import exception
from .api import BackendArguments
from .api import BackendFormatted
from .api import CachedValue
from .api import CacheMutex
from .api import CacheReturnType
from .api import CantDeserializeException
from .api import KeyType
from .api import MetaDataType
from .api import NO_VALUE
from .api import NoValueType
from .api import SerializedReturnType
from .api import Serializer
from .api import ValuePayload
from .backends import _backend_loader
from .backends import register_backend  # noqa
from .proxy import ProxyBackend
from .util import function_key_generator
from .util import function_multi_key_generator
from .util import repr_obj
from .. import Lock
from .. import NeedRegenerationException
from ..util import coerce_string_conf
from ..util import memoized_property
from ..util import NameRegistry
from ..util import PluginLoader
from ..util.typing import Self

value_version = 2
"""An integer placed in the :class:`.CachedValue`
so that new versions of dogpile.cache can detect cached
values from a previous, backwards-incompatible version.

"""

log = logging.getLogger(__name__)


AsyncCreator = Callable[
    ["CacheRegion", KeyType, Callable[[], ValuePayload], CacheMutex], None
]

ExpirationTimeCallable = Callable[[], float]

ToStr = Callable[[Any], str]

FunctionKeyGenerator = Callable[..., Callable[..., KeyType]]

FunctionMultiKeyGenerator = Callable[..., Callable[..., Sequence[KeyType]]]


class RegionInvalidationStrategy:
    """Region invalidation strategy interface

    Implement this interface and pass implementation instance
    to :meth:`.CacheRegion.configure` to override default region invalidation.

    Example::

        class CustomInvalidationStrategy(RegionInvalidationStrategy):

            def __init__(self):
                self._soft_invalidated = None
                self._hard_invalidated = None

            def invalidate(self, hard=None):
                if hard:
                    self._soft_invalidated = None
                    self._hard_invalidated = time.time()
                else:
                    self._soft_invalidated = time.time()
                    self._hard_invalidated = None

            def is_invalidated(self, timestamp):
                return ((self._soft_invalidated and
                         timestamp < self._soft_invalidated) or
                        (self._hard_invalidated and
                         timestamp < self._hard_invalidated))

            def was_hard_invalidated(self):
                return bool(self._hard_invalidated)

            def is_hard_invalidated(self, timestamp):
                return (self._hard_invalidated and
                        timestamp < self._hard_invalidated)

            def was_soft_invalidated(self):
                return bool(self._soft_invalidated)

            def is_soft_invalidated(self, timestamp):
                return (self._soft_invalidated and
                        timestamp < self._soft_invalidated)

    The custom implementation is injected into a :class:`.CacheRegion`
    at configure time using the
    :paramref:`.CacheRegion.configure.region_invalidator` parameter::

        region = CacheRegion()

        region = region.configure(region_invalidator=CustomInvalidationStrategy())  # noqa

    Invalidation strategies that wish to have access to the
    :class:`.CacheRegion` itself should construct the invalidator given the
    region as an argument::

        class MyInvalidator(RegionInvalidationStrategy):
            def __init__(self, region):
                self.region = region
                # ...

            # ...

        region = CacheRegion()
        region = region.configure(region_invalidator=MyInvalidator(region))

    .. versionadded:: 0.6.2

    .. seealso::

        :paramref:`.CacheRegion.configure.region_invalidator`

    """

    def invalidate(self, hard: bool = True) -> None:
        """Region invalidation.

        :class:`.CacheRegion` propagated call.
        The default invalidation system works by setting
        a current timestamp (using ``time.time()``) to consider all older
        timestamps effectively invalidated.

        """

        raise NotImplementedError()

    def is_hard_invalidated(self, timestamp: float) -> bool:
        """Check timestamp to determine if it was hard invalidated.

        :return: Boolean. True if ``timestamp`` is older than
         the last region invalidation time and region is invalidated
         in hard mode.

        """

        raise NotImplementedError()

    def is_soft_invalidated(self, timestamp: float) -> bool:
        """Check timestamp to determine if it was soft invalidated.

        :return: Boolean. True if ``timestamp`` is older than
         the last region invalidation time and region is invalidated
         in soft mode.

        """

        raise NotImplementedError()

    def is_invalidated(self, timestamp: float) -> bool:
        """Check timestamp to determine if it was invalidated.

        :return: Boolean. True if ``timestamp`` is older than
         the last region invalidation time.

        """

        raise NotImplementedError()

    def was_soft_invalidated(self) -> bool:
        """Indicate the region was invalidated in soft mode.

        :return: Boolean. True if region was invalidated in soft mode.

        """

        raise NotImplementedError()

    def was_hard_invalidated(self) -> bool:
        """Indicate the region was invalidated in hard mode.

        :return: Boolean. True if region was invalidated in hard mode.

        """

        raise NotImplementedError()


class DefaultInvalidationStrategy(RegionInvalidationStrategy):
    def __init__(self):
        self._is_hard_invalidated = None
        self._invalidated = None

    def invalidate(self, hard: bool = True) -> None:
        self._is_hard_invalidated = bool(hard)
        self._invalidated = time.time()

    def is_invalidated(self, timestamp: float) -> bool:
        return self._invalidated is not None and timestamp < self._invalidated

    def was_hard_invalidated(self) -> bool:
        return self._is_hard_invalidated is True

    def is_hard_invalidated(self, timestamp: float) -> bool:
        return self.was_hard_invalidated() and self.is_invalidated(timestamp)

    def was_soft_invalidated(self) -> bool:
        return self._is_hard_invalidated is False

    def is_soft_invalidated(self, timestamp: float) -> bool:
        return self.was_soft_invalidated() and self.is_invalidated(timestamp)


class CacheRegion:
    r"""A front end to a particular cache backend.

    :param name: Optional, a string name for the region.
     This isn't used internally
     but can be accessed via the ``.name`` parameter, helpful
     for configuring a region from a config file.
    :param function_key_generator:  Optional.  A
     function that will produce a "cache key" given
     a data creation function and arguments, when using
     the :meth:`.CacheRegion.cache_on_arguments` method.
     The structure of this function
     should be two levels: given the data creation function,
     return a new function that generates the key based on
     the given arguments.  Such as::

        def my_key_generator(namespace, fn, **kw):
            fname = fn.__name__
            def generate_key(*arg):
                return namespace + "_" + fname + "_".join(str(s) for s in arg)
            return generate_key


        region = make_region(
            function_key_generator = my_key_generator
        ).configure(
            "dogpile.cache.dbm",
            expiration_time=300,
            arguments={
                "filename":"file.dbm"
            }
        )

     The ``namespace`` is that passed to
     :meth:`.CacheRegion.cache_on_arguments`.  It's not consulted
     outside this function, so in fact can be of any form.
     For example, it can be passed as a tuple, used to specify
     arguments to pluck from \**kw::

        def my_key_generator(namespace, fn):
            def generate_key(*arg, **kw):
                return ":".join(
                        [kw[k] for k in namespace] +
                        [str(x) for x in arg]
                    )
            return generate_key


     Where the decorator might be used as::

        @my_region.cache_on_arguments(namespace=('x', 'y'))
        def my_function(a, b, **kw):
            return my_data()

     .. seealso::

        :func:`.function_key_generator` - default key generator

        :func:`.kwarg_function_key_generator` - optional gen that also
        uses keyword arguments

    :param function_multi_key_generator: Optional.
     Similar to ``function_key_generator`` parameter, but it's used in
     :meth:`.CacheRegion.cache_multi_on_arguments`. Generated function
     should return list of keys. For example::

        def my_multi_key_generator(namespace, fn, **kw):
            namespace = fn.__name__ + (namespace or '')

            def generate_keys(*args):
                return [namespace + ':' + str(a) for a in args]

            return generate_keys

    :param key_mangler: Function which will be used on all incoming
     keys before passing to the backend.  Defaults to ``None``,
     in which case the key mangling function recommended by
     the cache backend will be used.    A typical mangler
     is the SHA1 mangler found at :func:`.sha1_mangle_key`
     which coerces keys into a SHA1
     hash, so that the string length is fixed.  To
     disable all key mangling, set to ``False``.   Another typical
     mangler is the built-in Python function ``str``, which can be used
     to convert non-string or Unicode keys to bytestrings, which is
     needed when using a backend such as bsddb or dbm under Python 2.x
     in conjunction with Unicode keys.

    :param serializer: function which will be applied to all values before
     passing to the backend.  Defaults to ``None``, in which case the
     serializer recommended by the backend will be used.   Typical
     serializers include ``pickle.dumps`` and ``json.dumps``.

     .. versionadded:: 1.1.0

    :param deserializer: function which will be applied to all values returned
     by the backend.  Defaults to ``None``, in which case the
     deserializer recommended by the backend will be used.   Typical
     deserializers include ``pickle.dumps`` and ``json.dumps``.

     Deserializers can raise a :class:`.api.CantDeserializeException` if they
     are unable to deserialize the value from the backend, indicating
     deserialization failed and that caching should proceed to re-generate
     a value.  This allows an application that has been updated to gracefully
     re-cache old items which were persisted by a previous version of the
     application and can no longer be successfully deserialized.

     .. versionadded:: 1.1.0 added "deserializer" parameter

     .. versionadded:: 1.2.0 added support for
        :class:`.api.CantDeserializeException`

    :param async_creation_runner:  A callable that, when specified,
     will be passed to and called by dogpile.lock when
     there is a stale value present in the cache.  It will be passed the
     mutex and is responsible releasing that mutex when finished.
     This can be used to defer the computation of expensive creator
     functions to later points in the future by way of, for example, a
     background thread, a long-running queue, or a task manager system
     like Celery.

     For a specific example using async_creation_runner, new values can
     be created in a background thread like so::

        import threading

        def async_creation_runner(cache, somekey, creator, mutex):
            ''' Used by dogpile.core:Lock when appropriate  '''
            def runner():
                try:
                    value = creator()
                    cache.set(somekey, value)
                finally:
                    mutex.release()

            thread = threading.Thread(target=runner)
            thread.start()


        region = make_region(
            async_creation_runner=async_creation_runner,
        ).configure(
            'dogpile.cache.memcached',
            expiration_time=5,
            arguments={
                'url': '127.0.0.1:11211',
                'distributed_lock': True,
            }
        )

     Remember that the first request for a key with no associated
     value will always block; async_creator will not be invoked.
     However, subsequent requests for cached-but-expired values will
     still return promptly.  They will be refreshed by whatever
     asynchronous means the provided async_creation_runner callable
     implements.

     By default the async_creation_runner is disabled and is set
     to ``None``.

     .. versionadded:: 0.4.2 added the async_creation_runner
        feature.

    """

    def __init__(
        self,
        name: Optional[str] = None,
        function_key_generator: FunctionKeyGenerator = function_key_generator,
        function_multi_key_generator: FunctionMultiKeyGenerator = function_multi_key_generator,  # noqa E501
        key_mangler: Optional[Callable[[KeyType], KeyType]] = None,
        serializer: Optional[Callable[[ValuePayload], bytes]] = None,
        deserializer: Optional[Callable[[bytes], ValuePayload]] = None,
        async_creation_runner: Optional[AsyncCreator] = None,
    ):
        """Construct a new :class:`.CacheRegion`."""
        self.name = name
        self.function_key_generator = function_key_generator
        self.function_multi_key_generator = function_multi_key_generator
        self.key_mangler = self._user_defined_key_mangler = key_mangler
        self.serializer = self._user_defined_serializer = serializer
        self.deserializer = self._user_defined_deserializer = deserializer
        self.async_creation_runner = async_creation_runner
        self.region_invalidator: RegionInvalidationStrategy = (
            DefaultInvalidationStrategy()
        )

    def configure(
        self,
        backend: str,
        expiration_time: Optional[Union[float, datetime.timedelta]] = None,
        arguments: Optional[BackendArguments] = None,
        _config_argument_dict: Optional[Mapping[str, Any]] = None,
        _config_prefix: Optional[str] = None,
        wrap: Sequence[Union[ProxyBackend, Type[ProxyBackend]]] = (),
        replace_existing_backend: bool = False,
        region_invalidator: Optional[RegionInvalidationStrategy] = None,
    ) -> Self:
        """Configure a :class:`.CacheRegion`.

        The :class:`.CacheRegion` itself
        is returned.

        :param backend:   Required.  This is the name of the
         :class:`.CacheBackend` to use, and is resolved by loading
         the class from the ``dogpile.cache`` entrypoint.

        :param expiration_time:   Optional.  The expiration time passed
         to the dogpile system.  May be passed as an integer number
         of seconds, or as a ``datetime.timedelta`` value.

         .. versionadded 0.5.0
            ``expiration_time`` may be optionally passed as a
            ``datetime.timedelta`` value.

         The :meth:`.CacheRegion.get_or_create`
         method as well as the :meth:`.CacheRegion.cache_on_arguments`
         decorator (though note:  **not** the :meth:`.CacheRegion.get`
         method) will call upon the value creation function after this
         time period has passed since the last generation.

        :param arguments: Optional.  The structure here is passed
         directly to the constructor of the :class:`.CacheBackend`
         in use, though is typically a dictionary.

        :param wrap: Optional.  A list of :class:`.ProxyBackend`
         classes and/or instances, each of which will be applied
         in a chain to ultimately wrap the original backend,
         so that custom functionality augmentation can be applied.

         .. versionadded:: 0.5.0

         .. seealso::

            :ref:`changing_backend_behavior`

        :param replace_existing_backend: if True, the existing cache backend
         will be replaced.  Without this flag, an exception is raised if
         a backend is already configured.

         .. versionadded:: 0.5.7

        :param region_invalidator: Optional. Override default invalidation
         strategy with custom implementation of
         :class:`.RegionInvalidationStrategy`.

         .. versionadded:: 0.6.2

        """

        if "backend" in self.__dict__ and not replace_existing_backend:
            raise exception.RegionAlreadyConfigured(
                "This region is already "
                "configured with backend: %s.  "
                "Specify replace_existing_backend=True to replace."
                % self.backend
            )

        try:
            backend_cls = _backend_loader.load(backend)
        except PluginLoader.NotFound:
            raise exception.PluginNotFound(
                "Couldn't find cache plugin to load: %s" % backend
            )

        if _config_argument_dict:
            self.backend = backend_cls.from_config_dict(
                _config_argument_dict, _config_prefix
            )
        else:
            self.backend = backend_cls(arguments or {})

        self.expiration_time: Union[float, None]

        if not expiration_time or isinstance(expiration_time, Number):
            self.expiration_time = cast(Union[None, float], expiration_time)
        elif isinstance(expiration_time, datetime.timedelta):
            self.expiration_time = int(expiration_time.total_seconds())
        else:
            raise exception.ValidationError(
                "expiration_time is not a number or timedelta."
            )

        if not self._user_defined_key_mangler:
            self.key_mangler = self.backend.key_mangler

        if not self._user_defined_serializer:
            self.serializer = self.backend.serializer

        if not self._user_defined_deserializer:
            self.deserializer = self.backend.deserializer

        self._lock_registry = NameRegistry(self._create_mutex)

        if getattr(wrap, "__iter__", False):
            for wrapper in reversed(wrap):
                self.wrap(wrapper)

        if region_invalidator:
            self.region_invalidator = region_invalidator

        return self

    def wrap(self, proxy: Union[ProxyBackend, Type[ProxyBackend]]) -> None:
        """Takes a ProxyBackend instance or class and wraps the
        attached backend."""

        # if we were passed a type rather than an instance then
        # initialize it.
        if isinstance(proxy, type):
            proxy_instance = proxy()
        else:
            proxy_instance = proxy

        if not isinstance(proxy_instance, ProxyBackend):
            raise TypeError(
                "%r is not a valid ProxyBackend" % (proxy_instance,)
            )

        self.backend = proxy_instance.wrap(self.backend)

    def _mutex(self, key):
        return self._lock_registry.get(key)

    class _LockWrapper(CacheMutex):
        """weakref-capable wrapper for threading.Lock"""

        def __init__(self):
            self.lock = threading.Lock()

        def acquire(self, wait=True):
            return self.lock.acquire(wait)

        def release(self):
            self.lock.release()

        def locked(self):
            return self.lock.locked()

    def _create_mutex(self, key):
        mutex = self.backend.get_mutex(key)
        if mutex is not None:
            return mutex
        else:
            return self._LockWrapper()

    # cached value
    _actual_backend = None

    @property
    def actual_backend(self):
        """Return the ultimate backend underneath any proxies.

        The backend might be the result of one or more ``proxy.wrap``
        applications. If so, derive the actual underlying backend.

        .. versionadded:: 0.6.6

        """
        if self._actual_backend is None:
            _backend = self.backend
            while hasattr(_backend, "proxied"):
                _backend = _backend.proxied
            self._actual_backend = _backend
        return self._actual_backend

    def invalidate(self, hard=True):
        """Invalidate this :class:`.CacheRegion`.

        The default invalidation system works by setting
        a current timestamp (using ``time.time()``)
        representing the "minimum creation time" for
        a value.  Any retrieved value whose creation
        time is prior to this timestamp
        is considered to be stale.  It does not
        affect the data in the cache in any way, and is
        **local to this instance of :class:`.CacheRegion`.**

        .. warning::

            The :meth:`.CacheRegion.invalidate` method's default mode of
            operation is to set a timestamp **local to this CacheRegion
            in this Python process only**.   It does not impact other Python
            processes or regions as the timestamp is **only stored locally in
            memory**.  To implement invalidation where the
            timestamp is stored in the cache or similar so that all Python
            processes can be affected by an invalidation timestamp, implement a
            custom :class:`.RegionInvalidationStrategy`.

        Once set, the invalidation time is honored by
        the :meth:`.CacheRegion.get_or_create`,
        :meth:`.CacheRegion.get_or_create_multi` and
        :meth:`.CacheRegion.get` methods.

        The method supports both "hard" and "soft" invalidation
        options.  With "hard" invalidation,
        :meth:`.CacheRegion.get_or_create` will force an immediate
        regeneration of the value which all getters will wait for.
        With "soft" invalidation, subsequent getters will return the
        "old" value until the new one is available.

        Usage of "soft" invalidation requires that the region or the method
        is given a non-None expiration time.

        .. versionadded:: 0.3.0

        :param hard: if True, cache values will all require immediate
         regeneration; dogpile logic won't be used.  If False, the
         creation time of existing values will be pushed back before
         the expiration time so that a return+regen will be invoked.

         .. versionadded:: 0.5.1

        """
        self.region_invalidator.invalidate(hard)

    def configure_from_config(self, config_dict, prefix):
        """Configure from a configuration dictionary
        and a prefix.

        Example::

            local_region = make_region()
            memcached_region = make_region()

            # regions are ready to use for function
            # decorators, but not yet for actual caching

            # later, when config is available
            myconfig = {
                "cache.local.backend":"dogpile.cache.dbm",
                "cache.local.arguments.filename":"/path/to/dbmfile.dbm",
                "cache.memcached.backend":"dogpile.cache.pylibmc",
                "cache.memcached.arguments.url":"127.0.0.1, 10.0.0.1",
            }
            local_region.configure_from_config(myconfig, "cache.local.")
            memcached_region.configure_from_config(myconfig,
                                                "cache.memcached.")

        """
        config_dict = coerce_string_conf(config_dict)
        return self.configure(
            config_dict["%sbackend" % prefix],
            expiration_time=config_dict.get(
                "%sexpiration_time" % prefix, None
            ),
            _config_argument_dict=config_dict,
            _config_prefix="%sarguments." % prefix,
            wrap=config_dict.get("%swrap" % prefix, None),
            replace_existing_backend=config_dict.get(
                "%sreplace_existing_backend" % prefix, False
            ),
        )

    @memoized_property
    def backend(self):
        raise exception.RegionNotConfigured(
            "No backend is configured on this region."
        )

    @property
    def is_configured(self):
        """Return True if the backend has been configured via the
        :meth:`.CacheRegion.configure` method already.

        .. versionadded:: 0.5.1

        """
        return "backend" in self.__dict__

    def get(
        self,
        key: KeyType,
        expiration_time: Optional[float] = None,
        ignore_expiration: bool = False,
    ) -> Union[ValuePayload, NoValueType]:
        """Return a value from the cache, based on the given key.

        If the value is not present, the method returns the token
        :data:`.api.NO_VALUE`. :data:`.api.NO_VALUE` evaluates to False, but is
        separate from ``None`` to distinguish between a cached value of
        ``None``.

        By default, the configured expiration time of the
        :class:`.CacheRegion`, or alternatively the expiration
        time supplied by the ``expiration_time`` argument,
        is tested against the creation time of the retrieved
        value versus the current time (as reported by ``time.time()``).
        If stale, the cached value is ignored and the :data:`.api.NO_VALUE`
        token is returned.  Passing the flag ``ignore_expiration=True``
        bypasses the expiration time check.

        .. versionchanged:: 0.3.0
           :meth:`.CacheRegion.get` now checks the value's creation time
           against the expiration time, rather than returning
           the value unconditionally.

        The method also interprets the cached value in terms
        of the current "invalidation" time as set by
        the :meth:`.invalidate` method.   If a value is present,
        but its creation time is older than the current
        invalidation time, the :data:`.api.NO_VALUE` token is returned.
        Passing the flag ``ignore_expiration=True`` bypasses
        the invalidation time check.

        .. versionadded:: 0.3.0
           Support for the :meth:`.CacheRegion.invalidate`
           method.

        :param key: Key to be retrieved. While it's typical for a key to be a
         string, it is ultimately passed directly down to the cache backend,
         before being optionally processed by the key_mangler function, so can
         be of any type recognized by the backend or by the key_mangler
         function, if present.

        :param expiration_time: Optional expiration time value
         which will supersede that configured on the :class:`.CacheRegion`
         itself.

         .. note:: The :paramref:`.CacheRegion.get.expiration_time`
            argument is **not persisted in the cache** and is relevant
            only to **this specific cache retrieval operation**, relative to
            the creation time stored with the existing cached value.
            Subsequent calls to :meth:`.CacheRegion.get` are **not** affected
            by this value.

         .. versionadded:: 0.3.0

        :param ignore_expiration: if ``True``, the value is returned
         from the cache if present, regardless of configured
         expiration times or whether or not :meth:`.invalidate`
         was called.

         .. versionadded:: 0.3.0

        .. seealso::

            :meth:`.CacheRegion.get_multi`

            :meth:`.CacheRegion.get_or_create`

            :meth:`.CacheRegion.set`

            :meth:`.CacheRegion.delete`


        """
        value = self._get_cache_value(key, expiration_time, ignore_expiration)
        return value.payload

    def get_value_metadata(
        self,
        key: KeyType,
        expiration_time: Optional[float] = None,
        ignore_expiration: bool = False,
    ) -> Optional[CachedValue]:
        """Return the :class:`.CachedValue` object directly from the cache.

        This is the enclosing datastructure that includes the value as well as
        the metadata, including the timestamp when the value was cached.
        Convenience accessors on :class:`.CachedValue` also provide for common
        data such as :attr:`.CachedValue.cached_time` and
        :attr:`.CachedValue.age`.


        .. versionadded:: 1.3. Added :meth:`.CacheRegion.get_value_metadata`
        """
        cache_value = self._get_cache_value(
            key, expiration_time, ignore_expiration
        )
        if cache_value is NO_VALUE:
            return None
        else:
            if TYPE_CHECKING:
                assert isinstance(cache_value, CachedValue)
            return cache_value

    def _get_cache_value(
        self,
        key: KeyType,
        expiration_time: Optional[float] = None,
        ignore_expiration: bool = False,
    ) -> CacheReturnType:
        if self.key_mangler:
            key = self.key_mangler(key)
        value = self._get_from_backend(key)
        value = self._unexpired_value_fn(expiration_time, ignore_expiration)(
            value
        )
        return value

    def _unexpired_value_fn(self, expiration_time, ignore_expiration):
        if ignore_expiration:
            return lambda value: value
        else:
            if expiration_time is None:
                expiration_time = self.expiration_time

            current_time = time.time()

            def value_fn(value):
                if value is NO_VALUE:
                    return value
                elif (
                    expiration_time is not None
                    and current_time - value.metadata["ct"] > expiration_time
                ):
                    return NO_VALUE
                elif self.region_invalidator.is_invalidated(
                    value.metadata["ct"]
                ):
                    return NO_VALUE
                else:
                    return value

            return value_fn

    def get_multi(self, keys, expiration_time=None, ignore_expiration=False):
        """Return multiple values from the cache, based on the given keys.

        Returns values as a list matching the keys given.

        E.g.::

            values = region.get_multi(["one", "two", "three"])

        To convert values to a dictionary, use ``zip()``::

            keys = ["one", "two", "three"]
            values = region.get_multi(keys)
            dictionary = dict(zip(keys, values))

        Keys which aren't present in the list are returned as
        the ``NO_VALUE`` token.  ``NO_VALUE`` evaluates to False,
        but is separate from
        ``None`` to distinguish between a cached value of ``None``.

        By default, the configured expiration time of the
        :class:`.CacheRegion`, or alternatively the expiration
        time supplied by the ``expiration_time`` argument,
        is tested against the creation time of the retrieved
        value versus the current time (as reported by ``time.time()``).
        If stale, the cached value is ignored and the ``NO_VALUE``
        token is returned.  Passing the flag ``ignore_expiration=True``
        bypasses the expiration time check.

        .. versionadded:: 0.5.0

        """
        if not keys:
            return []

        if self.key_mangler is not None:
            keys = [self.key_mangler(key) for key in keys]

        backend_values = self._get_multi_from_backend(keys)

        _unexpired_value_fn = self._unexpired_value_fn(
            expiration_time, ignore_expiration
        )
        return [
            value.payload if value is not NO_VALUE else value
            for value in (
                _unexpired_value_fn(value) for value in backend_values
            )
        ]

    @contextlib.contextmanager
    def _log_time(self, keys):
        start_time = time.time()
        yield
        seconds = time.time() - start_time
        log.debug(
            "Cache value generated in %(seconds).3f seconds for key(s): "
            "%(keys)r",
            {"seconds": seconds, "keys": repr_obj(keys)},
        )

    def _is_cache_miss(self, value, orig_key):
        if value is NO_VALUE:
            log.debug("No value present for key: %r", orig_key)
        elif value.metadata["v"] != value_version:
            log.debug("Dogpile version update for key: %r", orig_key)
        elif self.region_invalidator.is_hard_invalidated(value.metadata["ct"]):
            log.debug("Hard invalidation detected for key: %r", orig_key)
        else:
            return False

        return True

    def key_is_locked(self, key: KeyType) -> bool:
        """Return True if a particular cache key is currently being generated
        within the dogpile lock.

        .. versionadded:: 1.1.2

        """
        mutex = self._mutex(key)
        locked: bool = mutex.locked()
        return locked

    def get_or_create(
        self,
        key: KeyType,
        creator: Callable[..., ValuePayload],
        expiration_time: Optional[float] = None,
        should_cache_fn: Optional[Callable[[ValuePayload], bool]] = None,
        creator_args: Optional[Tuple[Any, Mapping[str, Any]]] = None,
    ) -> ValuePayload:
        """Return a cached value based on the given key.

        If the value does not exist or is considered to be expired
        based on its creation time, the given
        creation function may or may not be used to recreate the value
        and persist the newly generated value in the cache.

        Whether or not the function is used depends on if the
        *dogpile lock* can be acquired or not.  If it can't, it means
        a different thread or process is already running a creation
        function for this key against the cache.  When the dogpile
        lock cannot be acquired, the method will block if no
        previous value is available, until the lock is released and
        a new value available.  If a previous value
        is available, that value is returned immediately without blocking.

        If the :meth:`.invalidate` method has been called, and
        the retrieved value's timestamp is older than the invalidation
        timestamp, the value is unconditionally prevented from
        being returned.  The method will attempt to acquire the dogpile
        lock to generate a new value, or will wait
        until the lock is released to return the new value.

        .. versionchanged:: 0.3.0
          The value is unconditionally regenerated if the creation
          time is older than the last call to :meth:`.invalidate`.

        :param key: Key to be retrieved. While it's typical for a key to be a
         string, it is ultimately passed directly down to the cache backend,
         before being optionally processed by the key_mangler function, so can
         be of any type recognized by the backend or by the key_mangler
         function, if present.

        :param creator: function which creates a new value.

        :param creator_args: optional tuple of (args, kwargs) that will be
         passed to the creator function if present.

         .. versionadded:: 0.7.0

        :param expiration_time: optional expiration time which will override
         the expiration time already configured on this :class:`.CacheRegion`
         if not None.   To set no expiration, use the value -1.

         .. note:: The :paramref:`.CacheRegion.get_or_create.expiration_time`
            argument is **not persisted in the cache** and is relevant
            only to **this specific cache retrieval operation**, relative to
            the creation time stored with the existing cached value.
            Subsequent calls to :meth:`.CacheRegion.get_or_create` are **not**
            affected by this value.

        :param should_cache_fn: optional callable function which will receive
         the value returned by the "creator", and will then return True or
         False, indicating if the value should actually be cached or not.  If
         it returns False, the value is still returned, but isn't cached.
         E.g.::

            def dont_cache_none(value):
                return value is not None

            value = region.get_or_create("some key",
                                create_value,
                                should_cache_fn=dont_cache_none)

         Above, the function returns the value of create_value() if
         the cache is invalid, however if the return value is None,
         it won't be cached.

         .. versionadded:: 0.4.3

        .. seealso::

            :meth:`.CacheRegion.get`

            :meth:`.CacheRegion.cache_on_arguments` - applies
            :meth:`.get_or_create` to any function using a decorator.

            :meth:`.CacheRegion.get_or_create_multi` - multiple key/value
            version

        """
        orig_key = key
        if self.key_mangler:
            key = self.key_mangler(key)

        def get_value():
            value = self._get_from_backend(key)
            if self._is_cache_miss(value, orig_key):
                raise NeedRegenerationException()

            ct = cast(CachedValue, value).metadata["ct"]
            if self.region_invalidator.is_soft_invalidated(ct):
                if expiration_time is None:
                    raise exception.DogpileCacheException(
                        "Non-None expiration time required "
                        "for soft invalidation"
                    )
                ct = time.time() - expiration_time - 0.0001

            return value.payload, ct

        def gen_value():
            with self._log_time(orig_key):
                if creator_args:
                    created_value = creator(
                        *creator_args[0], **creator_args[1]
                    )
                else:
                    created_value = creator()
            if inspect.iscoroutinefunction(creator):
                try:
                    created_value.send(None)
                except StopIteration as e:
                    created_value = e.value
            value = self._value(created_value)

            if (
                expiration_time is None
                and self.region_invalidator.was_soft_invalidated()
            ):
                raise exception.DogpileCacheException(
                    "Non-None expiration time required "
                    "for soft invalidation"
                )

            if not should_cache_fn or should_cache_fn(created_value):
                self._set_cached_value_to_backend(key, value)

            return value.payload, value.metadata["ct"]

        if expiration_time is None:
            expiration_time = self.expiration_time

        if expiration_time == -1:
            expiration_time = None

        async_creator: Optional[Callable[[CacheMutex], AsyncCreator]]
        if self.async_creation_runner:
            acr = self.async_creation_runner

            def async_creator(mutex):
                if creator_args:
                    ca = creator_args

                    @wraps(creator)
                    def go():
                        return creator(*ca[0], **ca[1])

                else:
                    go = creator  # type: ignore
                return acr(self, orig_key, go, mutex)

        else:
            async_creator = None

        with Lock(
            self._mutex(key),
            gen_value,
            get_value,
            expiration_time,
            async_creator,
        ) as value:
            if inspect.iscoroutinefunction(creator):
                async def coro_func(value):
                    return value
                return coro_func(value)
            return value

    def get_or_create_multi(
        self,
        keys: Sequence[KeyType],
        creator: Callable[[], ValuePayload],
        expiration_time: Optional[float] = None,
        should_cache_fn: Optional[Callable[[ValuePayload], bool]] = None,
    ) -> Sequence[ValuePayload]:
        """Return a sequence of cached values based on a sequence of keys.

        The behavior for generation of values based on keys corresponds
        to that of :meth:`.Region.get_or_create`, with the exception that
        the ``creator()`` function may be asked to generate any subset of
        the given keys.   The list of keys to be generated is passed to
        ``creator()``, and ``creator()`` should return the generated values
        as a sequence corresponding to the order of the keys.

        The method uses the same approach as :meth:`.Region.get_multi`
        and :meth:`.Region.set_multi` to get and set values from the
        backend.

        If you are using a :class:`.CacheBackend` or :class:`.ProxyBackend`
        that modifies values, take note this function invokes
        ``.set_multi()`` for newly generated values using the same values it
        returns to the calling function. A correct implementation of
        ``.set_multi()`` will not modify values in-place on the submitted
        ``mapping`` dict.

        :param keys: Sequence of keys to be retrieved.

        :param creator: function which accepts a sequence of keys and
         returns a sequence of new values.

        :param expiration_time: optional expiration time which will override
         the expiration time already configured on this :class:`.CacheRegion`
         if not None.   To set no expiration, use the value -1.

        :param should_cache_fn: optional callable function which will receive
         each value returned by the "creator", and will then return True or
         False, indicating if the value should actually be cached or not.  If
         it returns False, the value is still returned, but isn't cached.

        .. versionadded:: 0.5.0

        .. seealso::


            :meth:`.CacheRegion.cache_multi_on_arguments`

            :meth:`.CacheRegion.get_or_create`

        """

        def get_value(key):
            value = values.get(key, NO_VALUE)

            if self._is_cache_miss(value, orig_key):
                # dogpile.core understands a 0 here as
                # "the value is not available", e.g.
                # _has_value() will return False.
                return value.payload, 0
            else:
                ct = cast(CachedValue, value).metadata["ct"]
                if self.region_invalidator.is_soft_invalidated(ct):
                    if expiration_time is None:
                        raise exception.DogpileCacheException(
                            "Non-None expiration time required "
                            "for soft invalidation"
                        )
                    ct = time.time() - expiration_time - 0.0001

                return value.payload, ct

        def gen_value() -> ValuePayload:
            raise NotImplementedError()

        def async_creator(mutexes, key, mutex):
            mutexes[key] = mutex

        if expiration_time is None:
            expiration_time = self.expiration_time

        if expiration_time == -1:
            expiration_time = None

        sorted_unique_keys = sorted(set(keys))

        if self.key_mangler:
            mangled_keys = [self.key_mangler(k) for k in sorted_unique_keys]
        else:
            mangled_keys = sorted_unique_keys

        orig_to_mangled = dict(zip(sorted_unique_keys, mangled_keys))

        values = dict(
            zip(mangled_keys, self._get_multi_from_backend(mangled_keys))
        )

        mutexes: Mapping[KeyType, Any] = {}

        for orig_key, mangled_key in orig_to_mangled.items():
            with Lock(
                self._mutex(mangled_key),
                gen_value,
                lambda: get_value(mangled_key),
                expiration_time,
                async_creator=lambda mutex: async_creator(
                    mutexes, orig_key, mutex
                ),
            ):
                pass
        try:
            if mutexes:
                # sort the keys, the idea is to prevent deadlocks.
                # though haven't been able to simulate one anyway.
                keys_to_get = sorted(mutexes)

                with self._log_time(keys_to_get):
                    new_values = creator(*keys_to_get)

                values_w_created = {
                    orig_to_mangled[k]: self._value(v)
                    for k, v in zip(keys_to_get, new_values)
                }

                if (
                    expiration_time is None
                    and self.region_invalidator.was_soft_invalidated()
                ):
                    raise exception.DogpileCacheException(
                        "Non-None expiration time required "
                        "for soft invalidation"
                    )

                if not should_cache_fn:
                    self._set_multi_cached_value_to_backend(values_w_created)

                else:
                    self._set_multi_cached_value_to_backend(
                        {
                            k: v
                            for k, v in values_w_created.items()
                            if should_cache_fn(v.payload)
                        }
                    )

                values.update(values_w_created)
            return [values[orig_to_mangled[k]].payload for k in keys]
        finally:
            for mutex in mutexes.values():
                mutex.release()

    def _value(
        self, value: Any, metadata: Optional[MetaDataType] = None
    ) -> CachedValue:
        """Return a :class:`.CachedValue` given a value."""

        if metadata is None:
            metadata = self._gen_metadata()
        return CachedValue(value, metadata)

    def _parse_serialized_from_backend(
        self, value: SerializedReturnType
    ) -> CacheReturnType:
        if value in (None, NO_VALUE):
            return NO_VALUE

        assert self.deserializer
        byte_value = cast(bytes, value)

        bytes_metadata, _, bytes_payload = byte_value.partition(b"|")
        metadata = json.loads(bytes_metadata)
        try:
            payload = self.deserializer(bytes_payload)
        except CantDeserializeException:
            return NO_VALUE
        else:
            return CachedValue(payload, metadata)

    def _serialize_cached_value_elements(
        self, payload: ValuePayload, metadata: MetaDataType
    ) -> bytes:
        serializer = cast(Serializer, self.serializer)

        return b"%b|%b" % (
            json.dumps(metadata).encode("ascii"),
            serializer(payload),
        )

    def _serialized_payload(
        self, payload: ValuePayload, metadata: Optional[MetaDataType] = None
    ) -> BackendFormatted:
        """Return a backend formatted representation of a value.

        If a serializer is in use then this will return a string representation
        with the value formatted by the serializer.

        """
        if metadata is None:
            metadata = self._gen_metadata()

        return self._serialize_cached_value_elements(payload, metadata)

    def _serialized_cached_value(self, value: CachedValue) -> BackendFormatted:
        """Return a backend formatted representation of a
        :class:`.CachedValue`.

        If a serializer is in use then this will return a string representation
        with the value formatted by the serializer.

        """

        assert self.serializer
        return self._serialize_cached_value_elements(
            value.payload, value.metadata
        )

    def _get_from_backend(self, key: KeyType) -> CacheReturnType:
        if self.deserializer:
            return self._parse_serialized_from_backend(
                self.backend.get_serialized(key)
            )
        else:
            return cast(CacheReturnType, self.backend.get(key))

    def _get_multi_from_backend(
        self, keys: Sequence[KeyType]
    ) -> Sequence[CacheReturnType]:
        if self.deserializer:
            return [
                self._parse_serialized_from_backend(v)
                for v in self.backend.get_serialized_multi(keys)
            ]
        else:
            return cast(
                Sequence[CacheReturnType], self.backend.get_multi(keys)
            )

    def _set_cached_value_to_backend(
        self, key: KeyType, value: CachedValue
    ) -> None:
        if self.serializer:
            self.backend.set_serialized(
                key, self._serialized_cached_value(value)
            )
        else:
            self.backend.set(key, value)

    def _set_multi_cached_value_to_backend(
        self, mapping: Mapping[KeyType, CachedValue]
    ) -> None:
        if not mapping:
            return

        if self.serializer:
            self.backend.set_serialized_multi(
                {
                    k: self._serialized_cached_value(v)
                    for k, v in mapping.items()
                }
            )
        else:
            self.backend.set_multi(mapping)

    def _gen_metadata(self) -> MetaDataType:
        return {"ct": time.time(), "v": value_version}

    def set(self, key: KeyType, value: ValuePayload) -> None:
        """Place a new value in the cache under the given key."""

        if self.key_mangler:
            key = self.key_mangler(key)

        if self.serializer:
            self.backend.set_serialized(key, self._serialized_payload(value))
        else:
            self.backend.set(key, self._value(value))

    def set_multi(self, mapping: Mapping[KeyType, ValuePayload]) -> None:
        """Place new values in the cache under the given keys."""
        if not mapping:
            return

        metadata = self._gen_metadata()

        if self.serializer:
            if self.key_mangler:
                mapping = {
                    self.key_mangler(k): self._serialized_payload(
                        v, metadata=metadata
                    )
                    for k, v in mapping.items()
                }
            else:
                mapping = {
                    k: self._serialized_payload(v, metadata=metadata)
                    for k, v in mapping.items()
                }
            self.backend.set_serialized_multi(mapping)
        else:
            if self.key_mangler:
                mapping = {
                    self.key_mangler(k): self._value(v, metadata=metadata)
                    for k, v in mapping.items()
                }
            else:
                mapping = {
                    k: self._value(v, metadata=metadata)
                    for k, v in mapping.items()
                }
            self.backend.set_multi(mapping)

    def delete(self, key: KeyType) -> None:
        """Remove a value from the cache.

        This operation is idempotent (can be called multiple times, or on a
        non-existent key, safely)
        """

        if self.key_mangler:
            key = self.key_mangler(key)

        self.backend.delete(key)

    def delete_multi(self, keys: Sequence[KeyType]) -> None:
        """Remove multiple values from the cache.

        This operation is idempotent (can be called multiple times, or on a
        non-existent key, safely)

        .. versionadded:: 0.5.0

        """

        if self.key_mangler:
            km = self.key_mangler
            keys = [km(key) for key in keys]

        self.backend.delete_multi(keys)

    def cache_on_arguments(
        self,
        namespace: Optional[str] = None,
        expiration_time: Union[float, ExpirationTimeCallable, None] = None,
        should_cache_fn: Optional[Callable[[ValuePayload], bool]] = None,
        to_str: Callable[[Any], str] = str,
        function_key_generator: Optional[FunctionKeyGenerator] = None,
    ) -> Callable[[Callable[..., ValuePayload]], Callable[..., ValuePayload]]:
        """A function decorator that will cache the return
        value of the function using a key derived from the
        function itself and its arguments.

        The decorator internally makes use of the
        :meth:`.CacheRegion.get_or_create` method to access the
        cache and conditionally call the function.  See that
        method for additional behavioral details.

        E.g.::

            @someregion.cache_on_arguments()
            def generate_something(x, y):
                return somedatabase.query(x, y)

        The decorated function can then be called normally, where
        data will be pulled from the cache region unless a new
        value is needed::

            result = generate_something(5, 6)

        The function is also given an attribute ``invalidate()``, which
        provides for invalidation of the value.  Pass to ``invalidate()``
        the same arguments you'd pass to the function itself to represent
        a particular value::

            generate_something.invalidate(5, 6)

        Another attribute ``set()`` is added to provide extra caching
        possibilities relative to the function.   This is a convenience
        method for :meth:`.CacheRegion.set` which will store a given
        value directly without calling the decorated function.
        The value to be cached is passed as the first argument, and the
        arguments which would normally be passed to the function
        should follow::

            generate_something.set(3, 5, 6)

        The above example is equivalent to calling
        ``generate_something(5, 6)``, if the function were to produce
        the value ``3`` as the value to be cached.

        .. versionadded:: 0.4.1 Added ``set()`` method to decorated function.

        Similar to ``set()`` is ``refresh()``.   This attribute will
        invoke the decorated function and populate a new value into
        the cache with the new value, as well as returning that value::

            newvalue = generate_something.refresh(5, 6)

        .. versionadded:: 0.5.0 Added ``refresh()`` method to decorated
           function.

        ``original()`` on other hand will invoke the decorated function
        without any caching::

            newvalue = generate_something.original(5, 6)

        .. versionadded:: 0.6.0 Added ``original()`` method to decorated
           function.

        Lastly, the ``get()`` method returns either the value cached
        for the given key, or the token ``NO_VALUE`` if no such key
        exists::

            value = generate_something.get(5, 6)

        .. versionadded:: 0.5.3 Added ``get()`` method to decorated
           function.

        The default key generation will use the name
        of the function, the module name for the function,
        the arguments passed, as well as an optional "namespace"
        parameter in order to generate a cache key.

        Given a function ``one`` inside the module
        ``myapp.tools``::

            @region.cache_on_arguments(namespace="foo")
            def one(a, b):
                return a + b

        Above, calling ``one(3, 4)`` will produce a
        cache key as follows::

            myapp.tools:one|foo|3 4

        The key generator will ignore an initial argument
        of ``self`` or ``cls``, making the decorator suitable
        (with caveats) for use with instance or class methods.
        Given the example::

            class MyClass:
                @region.cache_on_arguments(namespace="foo")
                def one(self, a, b):
                    return a + b

        The cache key above for ``MyClass().one(3, 4)`` will
        again produce the same cache key of ``myapp.tools:one|foo|3 4`` -
        the name ``self`` is skipped.

        The ``namespace`` parameter is optional, and is used
        normally to disambiguate two functions of the same
        name within the same module, as can occur when decorating
        instance or class methods as below::

            class MyClass:
                @region.cache_on_arguments(namespace='MC')
                def somemethod(self, x, y):
                    ""

            class MyOtherClass:
                @region.cache_on_arguments(namespace='MOC')
                def somemethod(self, x, y):
                    ""

        Above, the ``namespace`` parameter disambiguates
        between ``somemethod`` on ``MyClass`` and ``MyOtherClass``.
        Python class declaration mechanics otherwise prevent
        the decorator from having awareness of the ``MyClass``
        and ``MyOtherClass`` names, as the function is received
        by the decorator before it becomes an instance method.

        The function key generation can be entirely replaced
        on a per-region basis using the ``function_key_generator``
        argument present on :func:`.make_region` and
        :class:`.CacheRegion`. If defaults to
        :func:`.function_key_generator`.

        :param namespace: optional string argument which will be
         established as part of the cache key.   This may be needed
         to disambiguate functions of the same name within the same
         source file, such as those
         associated with classes - note that the decorator itself
         can't see the parent class on a function as the class is
         being declared.

        :param expiration_time: if not None, will override the normal
         expiration time.

         May be specified as a callable, taking no arguments, that
         returns a value to be used as the ``expiration_time``. This callable
         will be called whenever the decorated function itself is called, in
         caching or retrieving. Thus, this can be used to
         determine a *dynamic* expiration time for the cached function
         result.  Example use cases include "cache the result until the
         end of the day, week or time period" and "cache until a certain date
         or time passes".

        :param should_cache_fn: passed to :meth:`.CacheRegion.get_or_create`.

        :param to_str: callable, will be called on each function argument
         in order to convert to a string.  Defaults to ``str()``.  If the
         function accepts non-ascii unicode arguments on Python 2.x, the
         ``unicode()`` builtin can be substituted, but note this will
         produce unicode cache keys which may require key mangling before
         reaching the cache.

        :param function_key_generator: a function that will produce a
         "cache key". This function will supersede the one configured on the
         :class:`.CacheRegion` itself.

        .. seealso::

            :meth:`.CacheRegion.cache_multi_on_arguments`

            :meth:`.CacheRegion.get_or_create`

        """
        expiration_time_is_callable = callable(expiration_time)

        if function_key_generator is None:
            _function_key_generator = self.function_key_generator
        else:
            _function_key_generator = function_key_generator

        def get_or_create_for_user_func(key_generator, user_func, *arg, **kw):
            key = key_generator(*arg, **kw)

            timeout: Optional[float] = (
                cast(ExpirationTimeCallable, expiration_time)()
                if expiration_time_is_callable
                else cast(Optional[float], expiration_time)
            )
            return self.get_or_create(
                key, user_func, timeout, should_cache_fn, (arg, kw)
            )

        def cache_decorator(user_func):
            if to_str is cast(Callable[[Any], str], str):
                # backwards compatible
                key_generator = _function_key_generator(
                    namespace, user_func
                )  # type: ignore
            else:
                key_generator = _function_key_generator(
                    namespace, user_func, to_str
                )

            def refresh(*arg, **kw):
                """
                Like invalidate, but regenerates the value instead
                """
                key = key_generator(*arg, **kw)
                value = user_func(*arg, **kw)
                self.set(key, value)
                return value

            def invalidate(*arg, **kw):
                key = key_generator(*arg, **kw)
                self.delete(key)

            def set_(value, *arg, **kw):
                key = key_generator(*arg, **kw)
                self.set(key, value)

            def get(*arg, **kw):
                key = key_generator(*arg, **kw)
                return self.get(key)

            user_func.set = set_
            user_func.invalidate = invalidate
            user_func.get = get
            user_func.refresh = refresh
            user_func.original = user_func

            # Use `decorate` to preserve the signature of :param:`user_func`.

            return decorate(
                user_func, partial(get_or_create_for_user_func, key_generator)
            )

        return cache_decorator

    def cache_multi_on_arguments(
        self,
        namespace: Optional[str] = None,
        expiration_time: Union[float, ExpirationTimeCallable, None] = None,
        should_cache_fn: Optional[Callable[[ValuePayload], bool]] = None,
        asdict: bool = False,
        to_str: ToStr = str,
        function_multi_key_generator: Optional[
            FunctionMultiKeyGenerator
        ] = None,
    ) -> Callable[
        [Callable[..., Sequence[ValuePayload]]],
        Callable[
            ..., Union[Sequence[ValuePayload], Mapping[KeyType, ValuePayload]]
        ],
    ]:
        """A function decorator that will cache multiple return
        values from the function using a sequence of keys derived from the
        function itself and the arguments passed to it.

        This method is the "multiple key" analogue to the
        :meth:`.CacheRegion.cache_on_arguments` method.

        Example::

            @someregion.cache_multi_on_arguments()
            def generate_something(*keys):
                return [
                    somedatabase.query(key)
                    for key in keys
                ]

        The decorated function can be called normally.  The decorator
        will produce a list of cache keys using a mechanism similar to
        that of :meth:`.CacheRegion.cache_on_arguments`, combining the
        name of the function with the optional namespace and with the
        string form of each key.  It will then consult the cache using
        the same mechanism as that of :meth:`.CacheRegion.get_multi`
        to retrieve all current values; the originally passed keys
        corresponding to those values which aren't generated or need
        regeneration will be assembled into a new argument list, and
        the decorated function is then called with that subset of
        arguments.

        The returned result is a list::

            result = generate_something("key1", "key2", "key3")

        The decorator internally makes use of the
        :meth:`.CacheRegion.get_or_create_multi` method to access the
        cache and conditionally call the function.  See that
        method for additional behavioral details.

        Unlike the :meth:`.CacheRegion.cache_on_arguments` method,
        :meth:`.CacheRegion.cache_multi_on_arguments` works only with
        a single function signature, one which takes a simple list of
        keys as arguments.

        Like :meth:`.CacheRegion.cache_on_arguments`, the decorated function
        is also provided with a ``set()`` method, which here accepts a
        mapping of keys and values to set in the cache::

            generate_something.set({"k1": "value1",
                                    "k2": "value2", "k3": "value3"})

        ...an ``invalidate()`` method, which has the effect of deleting
        the given sequence of keys using the same mechanism as that of
        :meth:`.CacheRegion.delete_multi`::

            generate_something.invalidate("k1", "k2", "k3")

        ...a ``refresh()`` method, which will call the creation
        function, cache the new values, and return them::

            values = generate_something.refresh("k1", "k2", "k3")

        ...and a ``get()`` method, which will return values
        based on the given arguments::

            values = generate_something.get("k1", "k2", "k3")

        .. versionadded:: 0.5.3 Added ``get()`` method to decorated
           function.

        Parameters passed to :meth:`.CacheRegion.cache_multi_on_arguments`
        have the same meaning as those passed to
        :meth:`.CacheRegion.cache_on_arguments`.

        :param namespace: optional string argument which will be
         established as part of each cache key.

        :param expiration_time: if not None, will override the normal
         expiration time.  May be passed as an integer or a
         callable.

        :param should_cache_fn: passed to
         :meth:`.CacheRegion.get_or_create_multi`. This function is given a
         value as returned by the creator, and only if it returns True will
         that value be placed in the cache.

        :param asdict: if ``True``, the decorated function should return
         its result as a dictionary of keys->values, and the final result
         of calling the decorated function will also be a dictionary.
         If left at its default value of ``False``, the decorated function
         should return its result as a list of values, and the final
         result of calling the decorated function will also be a list.

         When ``asdict==True`` if the dictionary returned by the decorated
         function is missing keys, those keys will not be cached.

        :param to_str: callable, will be called on each function argument
         in order to convert to a string.  Defaults to ``str()``.  If the
         function accepts non-ascii unicode arguments on Python 2.x, the
         ``unicode()`` builtin can be substituted, but note this will
         produce unicode cache keys which may require key mangling before
         reaching the cache.

        .. versionadded:: 0.5.0

        :param function_multi_key_generator: a function that will produce a
         list of keys. This function will supersede the one configured on the
         :class:`.CacheRegion` itself.

         .. versionadded:: 0.5.5

        .. seealso::

            :meth:`.CacheRegion.cache_on_arguments`

            :meth:`.CacheRegion.get_or_create_multi`

        """
        expiration_time_is_callable = callable(expiration_time)

        if function_multi_key_generator is None:
            _function_multi_key_generator = self.function_multi_key_generator
        else:
            _function_multi_key_generator = function_multi_key_generator

        def get_or_create_for_user_func(
            key_generator: Callable[..., Sequence[KeyType]],
            user_func: Callable[..., Sequence[ValuePayload]],
            *arg: Any,
            **kw: Any,
        ) -> Union[Sequence[ValuePayload], Mapping[KeyType, ValuePayload]]:
            cache_keys = arg
            keys = key_generator(*arg, **kw)
            key_lookup = dict(zip(keys, cache_keys))

            @wraps(user_func)
            def creator(*keys_to_create):
                return user_func(*[key_lookup[k] for k in keys_to_create])

            timeout: Optional[float] = (
                cast(ExpirationTimeCallable, expiration_time)()
                if expiration_time_is_callable
                else cast(Optional[float], expiration_time)
            )

            result: Union[
                Sequence[ValuePayload], Mapping[KeyType, ValuePayload]
            ]

            if asdict:

                def dict_create(*keys):
                    d_values = creator(*keys)
                    return [
                        d_values.get(key_lookup[k], NO_VALUE) for k in keys
                    ]

                def wrap_cache_fn(value):
                    if value is NO_VALUE:
                        return False
                    elif not should_cache_fn:
                        return True
                    else:
                        return should_cache_fn(value)

                result = self.get_or_create_multi(
                    keys, dict_create, timeout, wrap_cache_fn
                )
                result = dict(
                    (k, v)
                    for k, v in zip(cache_keys, result)
                    if v is not NO_VALUE
                )
            else:
                result = self.get_or_create_multi(
                    keys, creator, timeout, should_cache_fn
                )

            return result

        def cache_decorator(user_func):
            key_generator = _function_multi_key_generator(
                namespace, user_func, to_str=to_str
            )

            def invalidate(*arg):
                keys = key_generator(*arg)
                self.delete_multi(keys)

            def set_(mapping):
                keys = list(mapping)
                gen_keys = key_generator(*keys)
                self.set_multi(
                    dict(
                        (gen_key, mapping[key])
                        for gen_key, key in zip(gen_keys, keys)
                    )
                )

            def get(*arg):
                keys = key_generator(*arg)
                return self.get_multi(keys)

            def refresh(*arg):
                keys = key_generator(*arg)
                values = user_func(*arg)
                if asdict:
                    self.set_multi(dict(zip(keys, [values[a] for a in arg])))
                    return values
                else:
                    self.set_multi(dict(zip(keys, values)))
                    return values

            user_func.set = set_
            user_func.invalidate = invalidate
            user_func.refresh = refresh
            user_func.get = get

            # Use `decorate` to preserve the signature of :param:`user_func`.

            return decorate(
                user_func, partial(get_or_create_for_user_func, key_generator)
            )

        return cache_decorator


def make_region(*arg: Any, **kw: Any) -> CacheRegion:
    """Instantiate a new :class:`.CacheRegion`.

    Currently, :func:`.make_region` is a passthrough
    to :class:`.CacheRegion`.  See that class for
    constructor arguments.

    """
    return CacheRegion(*arg, **kw)
