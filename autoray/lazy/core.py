import operator
import threading
import functools
import itertools
import contextlib
import collections

from ..autoray import (
    shape,
    astype,
    get_dtype_name,
    get_lib_fn,
    infer_backend,
    multi_class_priorities,
    register_backend,
    register_function,
)
from .draw import (
    plot_graph,
    plot_circuit,
    plot_history_size_footprint,
    plot_history_functions,
    plot_history_stats,
)


_EMPTY_DICT = {}
get_depth = operator.attrgetter("_depth")


# ------------------------ traversal and computation ------------------------ #


def descend(lz):
    """Generate each unique computational node. Use ``ascend`` if you need to
    visit children before parents.

    Parameters
    ----------
    lz : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph to descend.

    Yields
    ------
    LazyArray
    """
    if isinstance(lz, LazyArray):
        queue = [lz]
    else:
        queue = sorted(lz, key=get_depth)

    seen = set()
    while queue:
        node = queue.pop()
        nid = id(node)
        if nid not in seen:
            yield node
            queue.extend(node._deps)
            seen.add(nid)


def ascend(lz):
    """Generate each unique computational node, from leaves to root. I.e. a
    topological ordering of the computational graph. Moreover, the nodes
    are visited 'shallowest first'.

    Parameters
    ----------
    lz : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph to ascend to.

    Yields
    ------
    LazyArray
    """
    if isinstance(lz, LazyArray):
        queue = [lz]
    else:
        queue = sorted(lz, key=get_depth)

    seen = set()
    ready = set()
    while queue:
        node = queue[-1]
        need_to_visit = [c for c in node._deps if id(c) not in ready]
        if need_to_visit:
            need_to_visit.sort(key=get_depth)
            queue.extend(need_to_visit)
        else:
            node = queue.pop()
            nid = id(node)
            ready.add(nid)
            if nid not in seen:
                yield node
                seen.add(nid)


def compute(lz):
    """Compute the value of one or more lazy arrays.

    Parameters
    ----------
    lz : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph to compute.

    Returns
    -------
    array or tuple of array
        The computed value(s) of the lazy array(s).
    """
    for node in ascend(lz):
        node._materialize()

    if isinstance(lz, LazyArray):
        return lz._data

    return tuple(node._data for node in lz)


def compute_constants(lz, variables):
    """Fold constant arrays - everything not dependent on ``variables`` -
    into the graph.

    Parameters
    ----------
    lz : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph.
    variables : LazyArray or sequence of LazyArray
        Nodes that should be treated as variable. I.e. any descendants will
        not be folded into the graph.
    """
    if isinstance(variables, LazyArray):
        variables = {variables}
    else:
        variables = set(variables)

    # must ascend
    for node in ascend(lz):
        if not any(c in variables for c in node._deps):
            # can fold
            node._materialize()
        else:
            # inherit variable status
            variables.add(node)


def get_source(lz, params=None):
    """Write the source code of an unravelled version of the computational
    graph, injecting required runtime objects into ``params``.

    Parameters
    ----------
    lz : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph to write the source code
        for. Their corresponding label is ``f"x{id(node)}"`` in the
        source code.

    Returns
    -------
    str
        The source code of the computational graph, suitable for ``exec``.
    """
    if params is None:
        # locals space mapping LazyArray names to values
        params = {}

    delete_checked = set()
    s = []  # source code lines

    for node in reversed(tuple(ascend(lz))):
        # when *descending*, the first encounter of a node is the
        # *last* time it is referenced in forward pass -> delete,
        # need to do this for GC since running in single big function
        for c in node._deps:
            if c not in delete_checked:
                if c._deps:
                    # is an intermediate - safe to delete. While we could
                    # delete input variables, we want to keep input *constants*
                    s.append(f"del x{id(c)}")
                delete_checked.add(c)

        if node._data is None:
            # create the array via computation
            s.append(node.as_string(params))
        else:
            # inject the already computed data as constant
            params[f"x{id(node)}"] = node._data

    # reverse (ascend) into source code
    return "\n".join(reversed(s))


class Function:
    """Get a compiled (by python ``compile``), function that performs the
    computational graph corresponding to ``inputs`` -> ``outputs``. The
    signature of the function is ``func(input_arrays) -> output_arrays``. As an
    intermediate step, the computational graph is traced to a flattened source
    code string.

    Parameters
    ----------
    inputs : LazyArray or sequence of LazyArray
        The input node(s) of the computational graph.
    outputs : LazyArray or sequence of LazyArray
        The output node(s) of the computational graph.
    fold_constants : bool, optional
        If True, fold constant arrays (those with no dependence on ``inputs``)
        into the graph ahead of compile.

    See Also
    --------
    get_source, compute
    """

    __slots__ = (
        "_in_names",
        "_out_names",
        "_source",
        "_code",
        "_params",
        "_is_single_in",
        "_is_single_out",
    )

    def __init__(self, inputs, outputs, fold_constants=True):
        if fold_constants:
            # compute everything not dependent on inputs
            compute_constants(outputs, variables=inputs)

        # write source and populate locals mapping that function will run under
        # params will include the functions and other constant objects
        self._params = {}
        self._source = get_source(outputs, params=self._params)

        # compile source
        self._code = compile(
            source=self._source,
            filename="<string>",
            mode="exec",
            optimize=1,
        )

        # get names to inject and extract arrays into and from locals
        self._is_single_in = isinstance(inputs, LazyArray)
        if self._is_single_in:
            self._in_names = f"x{id(inputs)}"
        else:
            self._in_names = tuple(f"x{id(v)}" for v in inputs)

        self._is_single_out = isinstance(outputs, LazyArray)
        if self._is_single_out:
            self._out_names = f"x{id(outputs)}"
        else:
            self._out_names = tuple(f"x{id(v)}" for v in outputs)

    def __call__(self, arrays, *args):

        # allow fn(arrays) or fn(*arrays)
        if args:
            arrays = (arrays,) + args

        # inject the new array(s)
        if self._is_single_in:
            self._params[self._in_names] = arrays
        else:
            for name, array in zip(self._in_names, arrays):
                self._params[name] = array

        # run the byte-compiled function with the updated locals
        exec(self._code, None, self._params)

        if self._is_single_in:
            # remove the input array(s) from the locals
            del self._params[self._in_names]
        else:
            for name in self._in_names:
                del self._params[name]

        if self._is_single_out:
            # return the result, whilst removing it from the locals
            return self._params.pop(self._out_names)

        # return the results, whilst removing them from the locals
        return tuple(self._params.pop(name) for name in self._out_names)

    def __getstate__(self):
        # can't pickle the code object -> recompile in setstate
        return (
            self._in_names,
            self._out_names,
            self._source,
            self._params,
            self._is_single_in,
            self._is_single_out,
        )

    def __setstate__(self, state):
        (
            self._in_names,
            self._out_names,
            self._source,
            self._params,
            self._is_single_in,
            self._is_single_out,
        ) = state

        # recompile the source
        self._code = compile(
            source=self._source,
            filename="<string>",
            mode="exec",
            optimize=1,
        )

    def print_source(self):
        """Print the source code of the compiled function."""
        print(self._source)

    def __repr__(self):
        if self._is_single_in:
            insig = "array_like"
        else:
            insig = "Sequence[array_like]"

        if self._is_single_out:
            outsig = "array_like"
        else:
            outsig = "Tuple[array_like]"

        return f"<Function({insig}) -> {outsig}>"


# --------------------------- computational nodes --------------------------- #


class LazyArray:
    """A lazy array representing a shaped node in a computational graph."""

    __slots__ = (
        "_backend",
        "_fn",
        "_args",
        "_kwargs",
        "_shape",
        "_data",
        "_deps",
        "_depth",
    )

    def __init__(
        self,
        backend,
        fn,
        args,
        kwargs,
        shape,
        deps=None,
    ):
        # info required to perform the computation
        self._backend = backend
        self._fn = fn
        self._args = args
        if kwargs is None:
            self._kwargs = _EMPTY_DICT
        else:
            self._kwargs = kwargs

        # resulting array information
        self._shape = shape
        self._data = None

        # lazy arrays this ``LazyArray`` depends on
        if deps is None:
            # automatically find them
            self._deps = (*find_lazy(self._args), *find_lazy(self._kwargs))
        else:
            # manually specified (slightly more efficient)
            self._deps = deps

        # tracking depth helps when ordering the computational graph
        if self._deps:
            self._depth = max(d._depth for d in self._deps) + 1
        else:
            self._depth = 0

    @classmethod
    def from_data(cls, data):
        """Create a new ``LazyArray`` directly from a concrete array."""
        obj = cls.__new__(cls)
        obj._backend = infer_backend(data)
        obj._fn = obj._args = obj._kwargs = None
        obj._shape = shape(data)
        obj._data = data
        obj._deps = ()
        obj._depth = 0
        return obj

    @classmethod
    def from_shape(cls, shape, backend="numpy"):
        """Create a new ``LazyArray`` with a given shape."""
        obj = cls.__new__(cls)
        obj._backend = backend
        obj._fn = obj._args = obj._kwargs = None
        obj._shape = tuple(map(int, shape))
        obj._data = "__PLACEHOLDER__"
        obj._deps = ()
        obj._depth = 0
        return obj

    def to(
        self,
        fn,
        args=None,
        kwargs=None,
        backend=None,
        shape=None,
        deps=None,
    ):
        """Create a new ``LazyArray``, by default propagating backend, shape,
        and deps from the the current LazyArray.
        """
        return LazyArray(
            fn=fn,
            args=args if args is not None else (self,),
            kwargs=kwargs,
            backend=backend if backend is not None else self._backend,
            shape=shape if shape is not None else self.shape,
            deps=deps if deps is not None else (self,),
        )

    def _materialize(self):
        """Recursively compute all required args and kwargs for this node
        before computing itself and dereferencing dependencies. Note using this
        to materialize a large computation from scratch should be avoided due
        to the recursion limit, use ``x.compute()`` instead.
        """
        if self._data is None:
            # materialize any actual array args
            args = (maybe_materialize(x) for x in self._args)
            kwargs = {k: maybe_materialize(v) for k, v in self._kwargs.items()}

            self._data = self._fn(*args, **kwargs)

            # free any references to deps
            self._fn = self._args = self._kwargs = None
            self._deps = ()

        return self._data

    __iter__ = descend = descend
    ascend = ascend

    def compute(self):
        """Compute the value of this lazy array.

        Unlike ``self._materialize()`` this avoids deep recursion.
        """
        for node in self.ascend():
            node._materialize()
        return self._data

    compute_constants = compute_constants

    def as_string(self, params):
        """Create a string which evaluates to the lazy array creation."""
        # name function and store in locals
        fn_name = f"{getattr(self._fn, '__name__', 'fn')}{id(self._fn)}"
        params.setdefault(fn_name, self._fn)

        # string of args and kwargs
        str_call = ", ".join(
            itertools.chain(
                (stringify(x, params) for x in self._args),
                (
                    f"{k}: {stringify(v, params)}"
                    for k, v in self._kwargs.items()
                ),
            )
        )

        # assign function call to new variable
        return f"x{id(self)} = {fn_name}({str_call})"

    get_source = get_source

    def get_function(self, variables, fold_constants=True):
        """Get a compiled function that computes ``fn(arrays)``, with ``fn``
        describing the computational graph of this ``LazyArray`` and ``arrays``
        corresponding to the downstream ``LazyArray`` nodes ``variables``.

        Parameters
        ----------
        variables : sequence of LazyArray
            Input nodes whose data can change between calls.
        fold_constants : bool, optional
            Compute all intermediates which do not depend on ``variables``
            prior to compilation.

        Returns
        -------
        fn : callable
            Function with signature ``fn(arrays)``.
        """
        return Function(
            inputs=variables, outputs=self, fold_constants=fold_constants
        )

    def show(self, filler=" ", max_lines=None, max_depth=None):
        """Show the computational graph as a nested directory structure."""
        if max_lines is None:
            max_lines = float("inf")
        if max_depth is None:
            max_depth = float("inf")

        # ┃ ━ ┗ ┣ │ ─ └ ╰ ├ ← ⬤
        bar = f"│{filler}"
        space = f"{filler}{filler}"
        junction = "├─"
        bend = "╰─"

        line = 0
        seen = {}
        queue = [(self, ())]
        while queue and (line < max_lines):
            t, columns = queue.pop()

            prefix = ""
            if columns:
                # work out various lines we need to draw based on whether the
                # sequence of parents are themselves the last child of their parent
                prefix += "".join(
                    bar if not p else space for p in columns[:-1]
                )
                prefix += bend if columns[-1] else junction

            if t.fn_name not in (None, "None"):
                item = f"{t.fn_name}{list(t.shape)}"
            else:
                # input node
                item = f"←{list(t.shape)}"

            if t in seen:
                # ignore loops, but point to when it was computed
                print(f"{line:>4} {prefix} ... ({item} from line {seen[t]})")
                line += 1
                continue

            print(f"{line:>4} {prefix}{item}")
            seen[t] = line
            line += 1

            if len(columns) < max_depth:
                deps = sorted(t.deps, key=get_depth, reverse=True)
                islasts = [True] + [False] * (len(deps) - 1)
                for islast, d in zip(islasts, deps):
                    queue.append((d, columns + (islast,)))

    def history_max_size(self):
        """Get the largest single tensor size appearing in this computation."""
        return max(node.size for node in self)

    def history_size_footprint(self):
        """Get the combined size of intermediates at each step of the
        computation. Note this assumes that intermediates are immediately
        garbage collected when they are no longer required.
        """
        delete_checked = set()
        sizes = []
        for node in reversed(tuple(self.ascend())):
            for c in node._deps:
                if c not in delete_checked:
                    # last time a dependency is seen, subtract the size
                    if c._deps:
                        sizes.append(-c.size)
                    delete_checked.add(c)

            if node._data is None:
                # this is a new intermediate, add the size
                sizes.append(+node.size)

        sizes.reverse()
        return list(itertools.accumulate(sizes))

    def history_peak_size(self):
        """Get the peak combined intermediate size of this computation."""
        return max(self.history_size_footprint())

    def history_total_size(self):
        """The the total size of all unique arrays in the computational graph,
        possibly relevant e.g. for back-propagation algorithms.
        """
        return sum(node.size for node in self)

    def history_stats(self, fn):
        """Compute aggregate statistics about the computational graph.

        Parameters
        ----------
        fn : callable or str
            Function to apply to each node in the computational graph. If a
            string, one of 'count', 'sizein', 'sizeout' can be used to count
            the number of nodes, the total size of the inputs, or the total
            size of each output respectively.

        Returns
        -------
        stats : dict
            Dictionary mapping function names to the aggregate statistics.
        """
        if not callable(fn):
            if fn == "count":

                def fn(node):
                    return 1

            elif fn == "sizein":

                def fn(node):
                    return sum(child.size for child in node.deps)

            elif fn == "sizeout":

                def fn(node):
                    return node.size

        stats = collections.defaultdict(int)
        for node in self:
            node_cost = fn(node)
            if node_cost is not None:
                stats[node.fn_name] += fn(node)

        return dict(stats)

    def history_fn_frequencies(self):
        """Get a dictionary mapping function names to the number of times they
        are used in the computational graph.
        """
        return self.history_stats("count")

    def to_nx_digraph(self, variables=None):
        """Convert this ``LazyArray`` into a ``networkx.DiGraph``."""
        import networkx as nx

        if variables is None:
            variables = set()
        elif isinstance(variables, LazyArray):
            variables = {variables}
        else:
            variables = set(variables)

        G = nx.DiGraph()
        for node in self.ascend():
            variable = (node in variables) or any(
                child in variables for child in node.deps
            )
            if variable:
                variables.add(node)
            G.add_node(node, variable=variable)
            for x in node.deps:
                G.add_edge(x, node)

        return G

    plot = plot_circuit
    plot_graph = plot_graph
    plot_circuit = plot_circuit
    plot_history_size_footprint = plot_history_size_footprint
    plot_history_functions = plot_history_functions
    plot_history_functions_scatter = functools.partialmethod(
        plot_history_functions, kind="scatter"
    )
    plot_history_functions_lines = functools.partialmethod(
        plot_history_functions, kind="lines"
    )
    plot_history_functions_image = functools.partialmethod(
        plot_history_functions, kind="image"
    )
    plot_history_stats = plot_history_stats
    plot_history_stats_counts = functools.partialmethod(
        plot_history_stats, fn="count"
    )
    plot_history_stats_sizein = functools.partialmethod(
        plot_history_stats, fn="sizein"
    )

    @property
    def fn(self):
        return self._fn

    @property
    def fn_name(self):
        return getattr(self._fn, "__name__", "None")

    @property
    def args(self):
        return self._args

    @property
    def kwargs(self):
        return self._kwargs

    @property
    def shape(self):
        return self._shape

    @property
    def ndim(self):
        return len(self._shape)

    @property
    def size(self):
        return functools.reduce(operator.mul, self.shape, 1)

    @property
    def backend(self):
        return self._backend

    @property
    def deps(self):
        return self._deps

    @property
    def depth(self):
        return self._depth

    def __getitem__(self, key):
        return getitem(self, key)

    # this makes numpy operations delegate to __rmatmul__ etc.
    __array_ufunc__ = None

    def __mul__(self, other):
        return multiply(self, other)

    def __rmul__(self, other):
        return multiply(self, other)

    def __add__(self, other):
        return add(self, other)

    def __radd__(self, other):
        return add(self, other)

    def __sub__(self, other):
        return sub(self, other)

    def __rsub__(self, other):
        return sub(other, self)

    def __floordiv__(self, other):
        return floordivide(self, other)

    def __rfloordiv__(self, other):
        return floordivide(other, self)

    def __truediv__(self, other):
        return truedivide(self, other)

    def __rtruediv__(self, other):
        return truedivide(other, self)

    def __pow__(self, other):
        return pow_(self, other)

    def __rpow__(self, other):
        return pow_(other, self)

    def __matmul__(self, other):
        return matmul(self, other)

    def __rmatmul__(self, other):
        return matmul(other, self)

    def __abs__(self):
        return abs_(self)

    def __neg__(self):
        return self.to(operator.neg)

    def __ne__(self, other):
        return ne(self, other)

    def __gt__(self, other):
        return gt(self, other)

    def __lt__(self, other):
        return lt(self, other)

    def __ge__(self, other):
        return ge(self, other)

    def __le__(self, other):
        return le(self, other)

    @property
    def T(self):
        return transpose(self)

    @property
    def H(self):
        return conj(transpose(self))

    def reshape(self, shape):
        return reshape(self, shape)

    def astype(self, dtype_name):
        return lazy_astype(self, dtype_name)

    @property
    def real(self):
        return real(self)

    @property
    def imag(self):
        return imag(self)

    def __repr__(self):
        return (
            f"<{self.__class__.__name__}("
            f"fn={self.fn_name}, "
            f"shape={self.shape}, "
            f"backend='{self.backend}')>"
        )


register_backend(LazyArray, "autoray.lazy")


def ensure_lazy(array):
    if not isinstance(array, LazyArray):
        return LazyArray.from_data(array)
    return array


def find_lazy(x):
    """Recursively search for ``LazyArray`` instances in pytrees."""
    if isinstance(x, LazyArray):
        yield x
        return

    if isinstance(x, (tuple, list)):
        for subx in x:
            yield from find_lazy(subx)
        return

    if isinstance(x, dict):
        for subx in x.values():
            yield from find_lazy(subx)
        return


# --------------------- recusively evaluating 'pytrees' --------------------- #


def materialize_larray(x):
    return x._materialize()


def materialize_tuple(x):
    return tuple(map(maybe_materialize, x))


def materialize_list(x):
    return list(map(maybe_materialize, x))


def materialize_dict(x):
    return {k: maybe_materialize(v) for k, v in x.items()}


def materialize_identity(x):
    return x


_materialize_dispatch = {
    LazyArray: materialize_larray,
    tuple: materialize_tuple,
    list: materialize_list,
    dict: materialize_dict,
}


def maybe_materialize(x):
    """Recursively evaluate LazyArray instances in tuples, lists and dicts."""
    try:
        return _materialize_dispatch[x.__class__](x)
    except KeyError:
        _materialize_dispatch[x.__class__] = materialize_identity
        return x


# -------------------- recusively stringifying 'pytrees' -------------------- #


def stringify_larray(x, params):
    name = f"x{id(x)}"
    if x._data is not None:
        params.setdefault(name, x._data)
    return name


def stringify_tuple(x, params):
    if not x:
        return "()"
    return f"({', '.join(stringify(xi, params) for xi in x)},)"


def stringify_list(x, params):
    return f"[{', '.join(stringify(xi, params) for xi in x)}]"


def stringify_dict(x, params):
    entries = (f"{k}: {stringify(v, params)}" for k, v in x.items())
    return f"{{{', '.join(entries)}}}"


def stringify_identity(x, params):
    if isinstance(x, (int, float, complex, bool, slice, range)):
        return f"{x}"
    if isinstance(x, str):
        return f"'{x}'"
    name = f"c{id(x)}"
    params.setdefault(name, x)
    return name


_stringify_dispatch = collections.defaultdict(
    lambda: stringify_identity,
    {
        LazyArray: stringify_larray,
        tuple: stringify_tuple,
        list: stringify_list,
        dict: stringify_dict,
    },
)


def stringify(x, params):
    """Recursively stringify LazyArray instances in tuples, lists and dicts."""
    return _stringify_dispatch[x.__class__](x, params)


# --------------------------------- caching --------------------------------- #


_SHARING_STACK = collections.defaultdict(list)


def currently_sharing():
    """Check if we are currently sharing a cache -- thread specific."""
    return threading.get_ident() in _SHARING_STACK


def get_sharing_cache():
    """Return the most recent sharing cache -- thread specific."""
    return _SHARING_STACK[threading.get_ident()][-1]


def _add_sharing_cache(cache):
    _SHARING_STACK[threading.get_ident()].append(cache)


def _remove_sharing_cache():
    tid = threading.get_ident()
    _SHARING_STACK[tid].pop()
    if not _SHARING_STACK[tid]:
        del _SHARING_STACK[tid]


@contextlib.contextmanager
def shared_intermediates(cache=None):
    """Context in which intermediate results are shared.

    Note that intermediate computations will not be garbage collected until
    1. this context exits, and
    2. the yielded cache is garbage collected (if it was captured).

    Parameters
    ----------
    cache : dict
        If specified, a user-stored dict in which intermediate results will
        be stored. This can be used to interleave sharing contexts.

    Returns
    -------
    cache : dict
        A dictionary in which sharing results are stored. If ignored,
        sharing results will be garbage collected when this context is
        exited. This dict can be passed to another context to resume
        sharing.
    """
    if cache is None:
        cache = {}
    _add_sharing_cache(cache)
    try:
        yield cache
    finally:
        _remove_sharing_cache()


def maybe_id(x):
    if hasattr(x, "shape"):
        return id(x)
    return x


def hash_args_kwargs(fn_name, *args, **kwargs):
    hargs = tuple(map(maybe_id, args))
    if kwargs:
        hkwargs = tuple(sorted((k, maybe_id(v)) for k, v in kwargs.items()))
    else:
        hkwargs = None
    return f"{fn_name}-{hash((hargs, hkwargs))}"


def lazy_cache(fn_name, hasher=None):
    """Decorator to mark a function as being lazy cacheable.

    Parameters
    ----------
    fn_name : str
        The name to use for the function in the cache.
    hasher : callable
        A function with signature ``hasher(fn_name, *args, **kwargs)`` that
        returns a hashable key for the cache. If not specified, the default
        is to use ``hash_args_kwargs``.
    """

    if hasher is None:
        hasher = hash_args_kwargs

    def wrapper(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not currently_sharing():
                return fn(*args, **kwargs)

            cache = get_sharing_cache()

            key = hasher(fn_name, *args, **kwargs)
            if key not in cache:
                cache[key] = fn(*args, **kwargs)

            return cache[key]

        return wrapped

    return wrapper


_DTYPES_REAL_EQUIV = {"complex128": "float64", "complex64": "float32"}
_DTYPES_COMPLEX_EQUIV = {"float64": "complex128", "float32": "complex64"}


@functools.lru_cache(None)
def dtype_real_equiv(dtype_name):
    return _DTYPES_REAL_EQUIV.get(dtype_name, dtype_name)


@functools.lru_cache(None)
def dtype_complex_equiv(dtype_name):
    return _DTYPES_COMPLEX_EQUIV.get(dtype_name, dtype_name)


@functools.lru_cache(None)
def _find_common_dtype(array_types, scalar_types):
    import numpy as np

    return np.find_common_type(array_types, scalar_types).name


def find_common_dtype(*xs):
    return _find_common_dtype(tuple(map(get_dtype_name, xs)), ())


@functools.lru_cache(None)
def _find_common_backend_cached(names):
    return max(
        names,
        key=lambda n: multi_class_priorities.get(n, 0),
    )


def find_common_backend(*xs):
    names = tuple(
        x.backend if isinstance(x, LazyArray) else infer_backend(x) for x in xs
    )
    return _find_common_backend_cached(names)


@functools.lru_cache(1024)
def find_broadcast_shape(xshape, yshape):
    xndim = len(xshape)
    yndim = len(yshape)
    if xndim < yndim:
        xshape = (1,) * (yndim - xndim)
    elif yndim < xndim:
        yshape = (1,) * (xndim - yndim)
    return tuple(max(d1, d2) for d1, d2 in zip(xshape, yshape))


# -------------------------------- interface -------------------------------- #


def Variable(shape, backend=None):
    """Create a ``LazyArray`` from a shape only, representing a leaf node
    in the computational graph. It can only act as a placeholder for data.
    """
    return LazyArray.from_shape(shape, backend=backend)


@lazy_cache("array")
def array(x):
    """Create a ``LazyArray`` from an input array, representing a leaf node
    in the computational graph.
    """
    return LazyArray.from_data(x)


@lazy_cache("transpose")
def transpose(a, axes=None):
    a = ensure_lazy(a)

    if axes is None:
        axes = range(a.ndim)[::-1]

    if all(i == ax for i, ax in enumerate(axes)):
        # no transposition required
        return a

    fn_transpose = get_lib_fn(a.backend, "transpose")
    oldshape = shape(a)
    newshape = tuple(oldshape[i] for i in axes)

    # check for chaining transpositions
    if a._fn is fn_transpose:
        b = a._args[0]
        if isinstance(b, LazyArray):
            axes_prev = a._args[1]
            axes_chained = tuple(axes_prev[k] for k in axes)
            return b.to(fn_transpose, (b, axes_chained), shape=newshape)

    return a.to(fn_transpose, (a, axes), shape=newshape)


@lazy_cache("reshape")
def _reshape_tuple(a, newshape):
    a = ensure_lazy(a)
    fn_reshape = get_lib_fn(a.backend, "reshape")

    # check for redundant reshapes
    if a._fn is fn_reshape:
        b = a._args[0]
        if isinstance(b, LazyArray):
            a = b

    return a.to(fn_reshape, (a, newshape), shape=newshape)


@functools.lru_cache(2**14)
def find_full_reshape(newshape, size):
    try:
        expand = newshape.index(-1)
        before = newshape[:expand]
        after = newshape[expand + 1 :]
        d = size // functools.reduce(
            operator.mul, itertools.chain(before, after), 1
        )
        return (*before, d, *after)
    except ValueError:
        return newshape


def reshape(a, newshape):
    newshape = (newshape,) if isinstance(newshape, int) else tuple(newshape)
    newshape = find_full_reshape(newshape, a.size)

    if shape(a) == tuple(newshape):
        # no reshape required
        return a

    return _reshape_tuple(a, newshape)


def getitem_hasher(_, a, key):
    if not isinstance(key, tuple):
        key = (key,)
    hkey = tuple(
        str(k) if isinstance(k, slice) else id(k) if hasattr(k, "shape") else k
        for k in key
    )
    return f"getitem-{hash((id(a), hkey))}"


@lazy_cache("getitem", hasher=getitem_hasher)
def getitem(a, key):
    a = ensure_lazy(a)

    deps = (a,)

    if not isinstance(key, tuple):
        key = (key,)

    try:
        # expand ellipsis
        expand = key.index(...)
        ndiff = a.ndim - len(key) + 1
        key = key[:expand] + (slice(None),) * ndiff + key[expand + 1 :]
    except ValueError:
        # else pad trailing slices if necessary
        ndiff = a.ndim - len(key)
        if ndiff:
            key = key + (slice(None),) * ndiff

    newshape = []
    for k, d in zip(key, shape(a)):
        if isinstance(k, LazyArray):
            newshape.append(k.size)
            deps += (k,)
        elif isinstance(k, slice):
            newshape.append(len(range(d)[k]))
        else:
            try:
                newshape = _get_py_shape(k)
            except TypeError:
                pass

    # TODO: np.newaxis == None

    newshape = tuple(newshape)
    return a.to(operator.getitem, (a, key), shape=newshape, deps=deps)


@lazy_cache("tensordot")
def tensordot(a, b, axes=2):
    if isinstance(axes, int):
        axes = (tuple(range(a.ndim - axes, a.ndim)), tuple(range(axes)))

    newshape = tuple(
        d for i, d in enumerate(shape(a)) if i not in axes[0]
    ) + tuple(d for i, d in enumerate(shape(b)) if i not in axes[1])

    backend = find_common_backend(a, b)
    fn_tensordot = get_lib_fn(backend, "tensordot")

    return LazyArray(
        backend=backend,
        fn=fn_tensordot,
        args=(a, b, axes),
        kwargs=None,
        shape=newshape,
        deps=tuple(x for x in (a, b) if isinstance(x, LazyArray)),
    )


@lazy_cache("einsum")
def einsum(*operands):
    from opt_einsum.parser import parse_einsum_input

    deps, output, larrays = parse_einsum_input(operands)

    size_dict = {}
    for term, op in zip(deps.split(","), larrays):
        op_shape = shape(op)
        for i, char in enumerate(term):
            size_dict[char] = max(size_dict.get(char, 1), op_shape[i])
    eq = deps + "->" + output
    newshape = tuple(size_dict[char] for char in output)

    backend = find_common_backend(*larrays)
    fn_einsum = get_lib_fn(backend, "einsum")

    return LazyArray(
        backend=backend,
        fn=fn_einsum,
        args=(eq, *larrays),
        kwargs=None,
        shape=newshape,
        deps=tuple(x for x in larrays if isinstance(x, LazyArray)),
    )


@lazy_cache("trace")
def trace(a):
    a = ensure_lazy(a)
    return a.to(
        fn=get_lib_fn(a.backend, "trace"),
        args=(a,),
        shape=(),
    )


@lazy_cache("diag")
def diag(a, k=0):
    a = ensure_lazy(a)

    if a.ndim == 1:
        new_d = shape(a)[0] + abs(k)
        new_shape = (new_d, new_d)
    elif a.ndim == 2:
        new_d = max(min(shape(a)) - abs(k), 0)
        new_shape = (new_d,)
    else:
        raise ValueError("Input must be 1- or 2-d.")

    return a.to(
        fn=get_lib_fn(a.backend, "diag"),
        args=(a, k),
        shape=new_shape,
    )


@lazy_cache("matmul")
def matmul(x1, x2):
    backend = find_common_backend(x1, x2)

    shape1 = shape(x1)
    shape2 = shape(x2)
    newshape = (*shape1[:-2], shape1[-2], shape2[-1])

    return LazyArray(
        backend=backend,
        fn=operator.matmul,
        args=(x1, x2),
        kwargs=None,
        shape=newshape,
        deps=tuple(x for x in (x1, x2) if isinstance(x, LazyArray)),
    )


@lazy_cache("kron")
def kron(x1, x2):
    backend = find_common_backend(x1, x2)
    shape1 = shape(x1)
    shape2 = shape(x2)
    newshape = tuple(d1 * d2 for d1, d2 in zip(shape1, shape2))
    fn_kron = get_lib_fn(backend, "kron")
    return LazyArray(
        backend=backend,
        fn=fn_kron,
        args=(x1, x2),
        kwargs=None,
        shape=newshape,
        deps=tuple(x for x in (x1, x2) if isinstance(x, LazyArray)),
    )


@lazy_cache("clip")
def clip(a, a_min, a_max):
    a = ensure_lazy(a)
    fn_clip = get_lib_fn(a.backend, "clip")
    return a.to(fn_clip, (a, a_min, a_max))


@lazy_cache("flip")
def flip(a, axis=None):
    a = ensure_lazy(a)
    fn_flip = get_lib_fn(a.backend, "flip")
    return a.to(fn_flip, (a, axis))


@lazy_cache("sort")
def sort(a, axis=-1):
    a = ensure_lazy(a)
    return a.to(get_lib_fn(a.backend, "sort"), (a, axis))


@lazy_cache("argsort")
def argsort(a, axis=-1):
    a = ensure_lazy(a)
    return a.to(
        fn=get_lib_fn(a.backend, "argsort"),
        args=(a, axis),
    )


@lazy_cache("stack")
def stack(arrays, axis=0):
    arrays = tuple(arrays)
    newshape = list(shape(arrays[0]))
    newshape.insert(axis if axis >= 0 else axis + 1, len(arrays))

    backend = find_common_backend(*arrays)
    fn = get_lib_fn(backend, "stack")
    return LazyArray(
        backend=backend,
        fn=fn,
        args=(arrays, axis),
        kwargs=None,
        shape=tuple(newshape),
        deps=tuple(x for x in arrays if isinstance(x, LazyArray)),
    )


@lazy_cache("concatenate")
def concatenate(arrays, axis=0):
    arrays = tuple(arrays)
    newshape = list(arrays[0].shape)
    newshape[axis] = sum(shape(a)[axis] for a in arrays)

    backend = infer_backend(arrays[0])
    fn = get_lib_fn(backend, "concatenate")
    return LazyArray(
        backend=backend,
        fn=fn,
        args=(arrays, axis),
        kwargs=None,
        shape=tuple(newshape),
        deps=tuple(x for x in arrays if isinstance(x, LazyArray)),
    )


@lazy_cache("split")
def split(ary, indices_or_sections, axis=0):
    ary = ensure_lazy(ary)

    d = shape(ary)[axis]
    num_subarrays = len(indices_or_sections) + 1
    div_points = [0] + list(indices_or_sections) + [d]

    sub_arys = []
    selector = [slice(None)] * ary.ndim
    for i in range(num_subarrays):
        st = div_points[i]
        end = div_points[i + 1]
        selector[axis] = slice(st, end)
        sub_arys.append(ary[tuple(selector)])

    return tuple(sub_arys)


def where(condition, x, y):
    x = ensure_lazy(x)
    condition = ensure_lazy(condition)
    return LazyArray(
        backend=find_common_backend(condition, x),
        fn=get_lib_fn(x.backend, "where"),
        args=(condition, x, y),
        kwargs=None,
        shape=find_broadcast_shape(condition.shape, x.shape),
        deps=tuple(a for a in (condition, x, y) if isinstance(a, LazyArray)),
    )


def _get_py_shape(x):
    """Infer the shape of a possibly nested list/tuple object."""
    if hasattr(x, "shape"):
        return list(x.shape)
    if isinstance(x, (tuple, list)):
        return [len(x)] + _get_py_shape(x[0])
    return []


@lazy_cache("take")
def take(x, indices):
    x = ensure_lazy(x)
    if isinstance(indices, (list, tuple)):
        new_shape = _get_py_shape(indices)
    else:
        indices = ensure_lazy(indices)
        new_shape = indices.shape
    return LazyArray(
        backend=x.backend,
        fn=get_lib_fn(x.backend, "take"),
        args=(x, indices),
        kwargs=None,
        shape=new_shape,
        deps=tuple(a for a in (x, indices) if isinstance(a, LazyArray)),
    )


def make_binary_func(name, fn):
    @lazy_cache(name)
    def binary_func(x1, x2):
        x1shape = getattr(x1, "shape", ())
        x2shape = getattr(x2, "shape", ())
        newshape = find_broadcast_shape(x1shape, x2shape)
        return LazyArray(
            backend=find_common_backend(x1, x2),
            fn=fn,
            args=(x1, x2),
            kwargs=None,
            shape=newshape,
            deps=tuple(x for x in (x1, x2) if isinstance(x, LazyArray)),
        )

    return binary_func


multiply = make_binary_func("multiply", operator.mul)
add = make_binary_func("add", operator.add)
sub = make_binary_func("sub", operator.sub)
floordivide = make_binary_func("floordivide", operator.floordiv)
truedivide = make_binary_func("truedivide", operator.truediv)
pow_ = make_binary_func("pow", operator.pow)
gt = make_binary_func("gt", operator.gt)
ne = make_binary_func("ne", operator.ne)
lt = make_binary_func("lt", operator.lt)
ge = make_binary_func("ge", operator.ge)
le = make_binary_func("le", operator.le)


def complex_(re, im):
    newshape = find_broadcast_shape(shape(re), shape(im))
    backend = find_common_backend(re, im)
    fn_complex = get_lib_fn(backend, "complex")
    return LazyArray(
        backend=backend,
        fn=fn_complex,
        args=(re, im),
        kwargs=None,
        shape=newshape,
        deps=tuple(x for x in (re, im) if isinstance(x, LazyArray)),
    )


def make_unary_func(name, to_real=False):
    @lazy_cache(name)
    def unary_func(x):
        x = ensure_lazy(x)
        return x.to(fn=get_lib_fn(x.backend, name))

    return unary_func


sin = make_unary_func("sin")
cos = make_unary_func("cos")
tan = make_unary_func("tan")
arcsin = make_unary_func("arcsin")
arccos = make_unary_func("arccos")
arctan = make_unary_func("arctan")
sinh = make_unary_func("sinh")
cosh = make_unary_func("cosh")
tanh = make_unary_func("tanh")
arcsinh = make_unary_func("arcsinh")
arccosh = make_unary_func("arccosh")
arctanh = make_unary_func("arctanh")
sqrt = make_unary_func("sqrt")
exp = make_unary_func("exp")
log = make_unary_func("log")
log2 = make_unary_func("log2")
log10 = make_unary_func("log10")
conj = make_unary_func("conj")
sign = make_unary_func("sign")
abs_ = make_unary_func("abs", to_real=True)
angle = make_unary_func("angle", to_real=True)
real = make_unary_func("real", to_real=True)
imag = make_unary_func("imag", to_real=True)


def make_reduction_func(name):
    @lazy_cache(name)
    def reduction_func(a, axis=None):
        a = ensure_lazy(a)
        fn = get_lib_fn(a.backend, name)

        nd = a.ndim
        if axis is None:
            return a.to(
                fn=fn,
                shape=(),
            )
        elif not hasattr(axis, "__len__"):
            axis = (axis,)
        axis = tuple(nd + i if i < 0 else i for i in axis)

        newshape = tuple(d for i, d in enumerate(shape(a)) if i not in axis)
        return a.to(fn=fn, args=(a, axis), shape=newshape)

    return reduction_func


sum_ = make_reduction_func("sum")
prod = make_reduction_func("prod")
min_ = make_reduction_func("min")
max_ = make_reduction_func("max")

# # XXX: still missing
# allclose, complex, diag
# dot, vdot, kron, inner, outer
# pad, eye
# squeeze, expand_dims
# to_numpy


# ---------------------------- autoray specials ----------------------------- #


def lazy_get_dtype_name(x):
    return x.dtype


@lazy_cache("astype")
def lazy_astype(x, dtype_name):
    x = ensure_lazy(x)
    return x.to(fn=astype, args=(x, dtype_name))


register_function("autoray.lazy", "get_dtype_name", lazy_get_dtype_name)
register_function("autoray.lazy", "astype", lazy_astype)
