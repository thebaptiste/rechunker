"""User-facing functions."""
import html
import textwrap
from collections import defaultdict
from typing import Union

import dask
import dask.array
import zarr
from packaging.version import Version

from rechunker.algorithm import rechunking_plan
from rechunker.pipeline import CopySpecToPipelinesMixin
from rechunker.types import ArrayProxy, CopySpec, CopySpecExecutor


class Rechunked:
    """
    A delayed rechunked result.

    This represents the rechunking plan, and when executed will perform
    the rechunking and return the rechunked array.

    Examples
    --------
    >>> source = zarr.ones((4, 4), chunks=(2, 2), store="source.zarr")
    >>> intermediate = "intermediate.zarr"
    >>> target = "target.zarr"
    >>> rechunked = rechunk(source, target_chunks=(4, 1), target_store=target,
    ...                     max_mem=256000,
    ...                     temp_store=intermediate)
    >>> rechunked
    <Rechunked>
    * Source      : <zarr.core.Array (4, 4) float64>
    * Intermediate: dask.array<from-zarr, ... >
    * Target      : <zarr.core.Array (4, 4) float64>
    >>> rechunked.execute()
    <zarr.core.Array (4, 4) float64>
    """

    def __init__(self, executor, plan, source, intermediate, target):
        self._executor = executor
        self._plan = plan
        self._source = source
        self._intermediate = intermediate
        self._target = target

    @property
    def plan(self):
        """Returns the executor-specific scheduling plan.

        The type of this object depends on the underlying execution engine.
        """
        return self._plan

    def execute(self, **kwargs):
        """
        Execute the rechunking.

        Parameters
        ----------
        **kwargs
            Keyword arguments are forwarded to the executor's ``execute_plan``
            method.

        Returns
        -------
        The same type of the ``source_array`` originally provided to
        :func:`rechunker.rechunk`.
        """
        self._executor.execute_plan(self._plan, **kwargs)
        return self._target

    def __repr__(self):
        entries = []
        entries.append(f"\n* Source      : {repr(self._source)}")
        if self._intermediate is not None:
            entries.append(f"\n* Intermediate: {repr(self._intermediate)}")
        entries.append(f"\n* Target      : {repr(self._target)}")
        entries = "\n".join(entries)
        return f"<Rechunked>{entries}\n"

    def _repr_html_(self):
        entries = {}
        for kind, obj in [
            ("source", self._source),
            ("intermediate", self._intermediate),
            ("target", self._target),
        ]:
            try:
                body = obj._repr_html_()
            except AttributeError:
                body = f"<p><code>{html.escape(repr(self._target))}</code></p>"
            entries[f"{kind}_html"] = body

        template = textwrap.dedent(
            """<h2>Rechunked</h2>\

        <details>
          <summary><b>Source</b></summary>
          {{source_html}}
        </details>
        {}
        <details>
          <summary><b>Target</b></summary>
          {{target_html}}
        </details>
        """
        )

        if self._intermediate is not None:
            intermediate = textwrap.dedent(
                """\
                <details>
                <summary><b>Intermediate</b></summary>
                {intermediate_html}
                </details>
            """
            )
        else:
            intermediate = ""
        template = template.format(intermediate)
        return template.format(**entries)


def _shape_dict_to_tuple(dims, shape_dict):
    # convert a dict of shape
    shape = [shape_dict[dim] for dim in dims]
    return tuple(shape)


def _get_dims_from_zarr_array(z_array):
    # use Xarray convention
    # http://xarray.pydata.org/en/stable/internals.html#zarr-encoding-specification
    return z_array.attrs["_ARRAY_DIMENSIONS"]


def _encode_zarr_attributes(attrs):
    from xarray.backends.zarr import encode_zarr_attr_value

    return {k: encode_zarr_attr_value(v) for k, v in attrs.items()}


def _zarr_empty(shape, store_or_group, chunks, dtype, name=None, **kwargs):
    # wrapper that maybe creates the array within a group
    if isinstance(store_or_group, zarr.Group):
        assert name is not None
        return store_or_group.empty(
            name, shape=shape, chunks=chunks, dtype=dtype, **kwargs
        )
    else:
        # ignore name
        return zarr.empty(
            shape, chunks=chunks, dtype=dtype, store=store_or_group, **kwargs
        )


ZARR_OPTIONS = [
    "compressor",
    "filters",
    "order",
    "cache_metadata",
    "cache_attrs",
    "overwrite",
    "write_empty_chunks",
]


def _validate_options(options):
    if not options:
        return
    for o in options:
        if o not in ZARR_OPTIONS:
            raise ValueError(
                f"Zarr options must not include {o} (got {o}={options[o]}). "
                f"Only the following options are supported: {ZARR_OPTIONS}."
            )


def _get_executor(name: str) -> CopySpecExecutor:
    # converts a string name into a Executor instance
    # imports are conditional to avoid hard dependencies
    if name.lower() == "dask":
        from rechunker.executors.dask import DaskPipelineExecutor

        class DaskCopySpecExecutor(DaskPipelineExecutor, CopySpecToPipelinesMixin):
            pass

        return DaskCopySpecExecutor()
    elif name.lower() == "beam":
        from rechunker.executors.beam import BeamExecutor

        return BeamExecutor()
    elif name.lower() == "prefect":
        from rechunker.executors.prefect import PrefectPipelineExecutor

        class PrefectCopySpecExecutor(
            PrefectPipelineExecutor, CopySpecToPipelinesMixin
        ):
            pass

        return PrefectCopySpecExecutor()
    elif name.lower() == "python":
        from rechunker.executors.python import PythonPipelineExecutor

        class PythonCopySpecExecutor(PythonPipelineExecutor, CopySpecToPipelinesMixin):
            pass

        return PythonCopySpecExecutor()
    elif name.lower() == "pywren":
        from rechunker.executors.pywren import PywrenExecutor

        return PywrenExecutor()
    else:
        raise ValueError(f"unrecognized executor {name}")


def rechunk(
    source,
    target_chunks,
    max_mem,
    target_store,
    target_options=None,
    temp_store=None,
    temp_options=None,
    executor: Union[str, CopySpecExecutor] = "dask",
    array_name=None,
) -> Rechunked:
    """
    Rechunk a Zarr Array or Group, a Dask Array, or an Xarray Dataset

    Parameters
    ----------
    source : zarr.Array, zarr.Group, dask.array.Array, or xarray.Dataset
        Named dimensions in the Zarr arrays will be parsed according to the
        Xarray :ref:`xarray:zarr_encoding`.
    target_chunks : tuple, dict, or None
        The desired chunks of the array after rechunking. The structure
        depends on ``source``.

        - For a single array source, ``target_chunks`` can
          be either a tuple (e.g. ``(20, 5, 3)``) or a dictionary
          (e.g. ``{'time': 20, 'lat': 5, 'lon': 3}``). Dictionary syntax
          requires the dimension names be present in the Zarr Array
          attributes (see Xarray :ref:`xarray:zarr_encoding`.)
          A value of ``None`` means that the array will
          be copied with no change to its chunk structure.
        - For a group of arrays, a dict is required. The keys correspond to array names.
          The values are ``target_chunks`` arguments for the array. For example,
          ``{'foo': (20, 10), 'bar': {'x': 3, 'y': 5}, 'baz': None}``.
          *All arrays you want to rechunk must be explicitly named.* Arrays
          that are not present in the ``target_chunks`` dict will be ignored.

    max_mem : str or int
        The amount of memory (in bytes) that workers are allowed to use. A
        string (e.g. ``100MB``) can also be used.
    target_store : str, MutableMapping, or zarr.Store object
        The location in which to store the final, rechunked result.
        Will be passed directly to :py:meth:`zarr.creation.create`
    target_options: Dict, optional
        Additional keyword arguments used to control array storage.
        If the source is :py:class:`xarray.Dataset`, then these options will be used
        to encode variables in the same manner as the ``encoding`` parameter in
        :py:meth:`xarray.Dataset.to_zarr`. Otherwise, these options will be passed
        to :py:meth:`zarr.creation.create`. The structure depends on ``source``.

        - For a single array source, this should be a single dict such
          as ``{'compressor': zarr.Blosc(), 'order': 'F'}``.
        - For a group of arrays, a nested dict is required with values
          like the above keyed by array name.  For example,
          ``{'foo': {'compressor': zarr.Blosc(), 'order': 'F'}, 'bar': {'compressor': None}}``.

    temp_store : str, MutableMapping, or zarr.Store object, optional
        Location of temporary store for intermediate data. Can be deleted
        once rechunking is complete.
    temp_options: Dict, optional
        Options with same semantics as ``target_options`` for ``temp_store`` rather than
        ``target_store``.  Defaults to ``target_options`` and has no effect when source
        is of type xarray.Dataset.
    executor: str or rechunker.types.Executor
        Implementation of the execution engine for copying between zarr arrays.
        Supplying a custom Executor is currently even more experimental than the
        rest of Rechunker: we expect the interface to evolve as we add more
        executors and make no guarantees of backwards compatibility. The
        currently implemented executors are

        * dask
        * beam
        * prefect
        * python
        * pywren

    array_name: str, optional
        Required when rechunking an array if any of the targets is a group


    Returns
    -------
    rechunked : :class:`Rechunked` object
    """
    if isinstance(executor, str):
        executor = _get_executor(executor)

    copy_spec, intermediate, target = _setup_rechunk(
        source=source,
        target_chunks=target_chunks,
        max_mem=max_mem,
        target_store=target_store,
        target_options=target_options,
        temp_store=temp_store,
        temp_options=temp_options,
        array_name=array_name,
    )
    plan = executor.prepare_plan(copy_spec)
    return Rechunked(executor, plan, source, intermediate, target)


def get_dim_chunk(da, dim, target_chunks):
    if dim in target_chunks.keys():
        if target_chunks[dim] > len(da[dim]) or target_chunks[dim] < 0:
            dim_chunk = len(da[dim])
        else:
            dim_chunk = target_chunks[dim]
    else:
        if not isinstance(da.data, dask.array.Array):
            dim_chunk = len(da[dim])
        else:
            existing_chunksizes = {k: v for k, v in zip(da.dims, da.data.chunksize)}
            dim_chunk = existing_chunksizes[dim]
    return dim_chunk


def parse_target_chunks_from_dim_chunks(ds, target_chunks):
    """
    Calculate ``target_chunks`` suitable for ``rechunker.rechunk()`` using chunks defined for
    dataset dimensions (similar to xarray's ``.rechunk()``) .

    - If a dimension is missing from ``target_chunks`` then use the full length from ``ds``.
    - If a chunk in ``target_chunks`` is larger than the full length of the variable in ``ds``,
      then, again, use the full length from the dataset.
    - If a dimension chunk is specified as -1, again, use the full length from the dataset.

    """
    group_chunks = defaultdict(list)

    for var in ds.variables:
        for dim in ds[var].dims:
            group_chunks[var].append(get_dim_chunk(ds[var], dim, target_chunks))

    # rechunk() expects chunks values to be a tuple. So let's convert them
    group_chunks_tuples = {var: tuple(chunks) for (var, chunks) in group_chunks.items()}
    return group_chunks_tuples


def _copy_group_attributes(source, target):
    """Visit every source group and create it on the target and move any attributes found."""

    def _update_group_attrs(name):
        if isinstance(source.get(name), zarr.Group):
            group = target.create_group(name)
            group.attrs.update(source.get(name).attrs)

    source.visit(_update_group_attrs)


def _setup_rechunk(
    source,
    target_chunks,
    max_mem,
    target_store,
    target_options=None,
    temp_store=None,
    temp_options=None,
    array_name=None,
):
    if temp_options is None:
        temp_options = target_options
    target_options = target_options or {}
    temp_options = temp_options or {}

    # import xarray dynamically since it is not a required dependency
    try:
        import xarray
        from xarray.backends.zarr import (
            DIMENSION_KEY,
            encode_zarr_attr_value,
            encode_zarr_variable,
            extract_zarr_variable_encoding,
        )
        from xarray.conventions import encode_dataset_coordinates
    except ImportError:
        xarray = None

    if xarray and isinstance(source, xarray.Dataset):
        if not isinstance(target_chunks, dict):
            raise ValueError(
                "You must specify ``target-chunks`` as a dict when rechunking a dataset."
            )
        if array_name is not None:
            raise ValueError(
                "Can't specify `array_name` when rechunking an Xarray Dataset."
            )

        variables, attrs = encode_dataset_coordinates(source)
        attrs = _encode_zarr_attributes(attrs)

        if temp_store is not None:
            if isinstance(temp_store, zarr.Group):
                temp_group = temp_store
            else:
                temp_group = zarr.group(temp_store)
        else:
            temp_group = None

        if isinstance(target_store, zarr.Group):
            target_group = target_store
        else:
            target_group = zarr.group(target_store)
        target_group.attrs.update(attrs)

        # if ``target_chunks`` is specified per dimension (xarray ``.rechunk`` style),
        # parse chunks for each coordinate/variable
        if all([k in source.dims for k in target_chunks.keys()]):
            # ! We can only apply this when all keys are indeed dimension, otherwise it falls back to the old method
            target_chunks = parse_target_chunks_from_dim_chunks(source, target_chunks)

        copy_specs = []
        for name, variable in variables.items():
            # This isn't strictly necessary because a shallow copy
            # also occurs in `encode_dataset_coordinates` but do it
            # anyways in case the coord encoding function changes
            variable = variable.copy()

            # Update the array encoding with provided options and apply it;
            # note that at this point the `options` may contain any valid property
            # applicable for the `encoding` parameter in Dataset.to_zarr other than "chunks"
            options = target_options.get(name, {})
            if "chunks" in options:
                raise ValueError(
                    f"Chunks must be provided in 'target_chunks' rather than options (variable={name})"
                )
            # Drop any leftover chunks encoding
            variable.encoding.pop("chunks", None)
            variable.encoding.update(options)
            variable = encode_zarr_variable(variable)

            # Extract the array encoding to get a default chunking, a step
            # which will also ensure that the target chunking is compatible
            # with the current chunking (only necessary for on-disk arrays)
            kws = {}
            if Version(xarray.__version__) >= Version("2025.03.1"):
                kws = {"zarr_format": 2}
            variable_encoding = extract_zarr_variable_encoding(
                variable, raise_on_invalid=False, name=name, **kws
            )
            variable_chunks = target_chunks.get(name, variable_encoding["chunks"])
            if isinstance(variable_chunks, dict):
                variable_chunks = _shape_dict_to_tuple(variable.dims, variable_chunks)

            # Restrict options to only those that are specific to zarr and
            # not managed internally
            options = {k: v for k, v in options.items() if k in ZARR_OPTIONS}
            _validate_options(options)

            # Extract array attributes along with reserved property for
            # xarray dimension names
            variable_attrs = _encode_zarr_attributes(variable.attrs)
            variable_attrs[DIMENSION_KEY] = encode_zarr_attr_value(variable.dims)

            copy_spec = _setup_array_rechunk(
                dask.array.asarray(variable),
                variable_chunks,
                max_mem,
                target_group,
                target_options=options,
                temp_store_or_group=temp_group,
                temp_options=options,
                name=name,
            )
            copy_spec.write.array.attrs.update(variable_attrs)  # type: ignore
            copy_specs.append(copy_spec)

        return copy_specs, temp_group, target_group

    elif isinstance(source, zarr.hierarchy.Group):
        if not isinstance(target_chunks, dict):
            raise ValueError(
                "You must specify ``target-chunks`` as a dict when rechunking a group."
            )
        if array_name is not None:
            raise ValueError("Can't specify `array_name` when rechunking a Group.")

        if temp_store is not None:
            if isinstance(temp_store, zarr.Group):
                temp_group = temp_store
            else:
                temp_group = zarr.group(temp_store)
        else:
            temp_group = None

        if isinstance(target_store, zarr.Group):
            target_group = target_store
        else:
            target_group = zarr.group(target_store)
        _copy_group_attributes(source, target_group)
        target_group.attrs.update(source.attrs)

        copy_specs = []
        for array_name, array_target_chunks in target_chunks.items():
            copy_spec = _setup_array_rechunk(
                source[array_name],
                array_target_chunks,
                max_mem,
                target_group,
                target_options=target_options.get(array_name),
                temp_store_or_group=temp_group,
                temp_options=temp_options.get(array_name),
                name=array_name,
            )
            copy_specs.append(copy_spec)

        return copy_specs, temp_group, target_group

    elif isinstance(source, (zarr.core.Array, dask.array.Array)):
        if (
            isinstance(target_store, zarr.Group) or isinstance(temp_store, zarr.Group)
        ) and array_name is None:
            raise ValueError("Can't rechunk to a group without a name for the array.")

        copy_spec = _setup_array_rechunk(
            source,
            target_chunks,
            max_mem,
            target_store,
            target_options=target_options,
            temp_store_or_group=temp_store,
            temp_options=temp_options,
            name=array_name,
        )
        intermediate = copy_spec.intermediate.array
        target = copy_spec.write.array
        return [copy_spec], intermediate, target

    else:
        raise ValueError(
            f"Source must be a Zarr Array, Zarr Group, Dask Array or Xarray Dataset (not {type(source)})."
        )


def _setup_array_rechunk(
    source_array,
    target_chunks,
    max_mem,
    target_store_or_group,
    target_options=None,
    temp_store_or_group=None,
    temp_options=None,
    name=None,
) -> CopySpec:
    _validate_options(target_options)
    _validate_options(temp_options)
    shape = source_array.shape
    source_chunks = (
        source_array.chunksize
        if isinstance(source_array, dask.array.Array)
        else source_array.chunks
    )
    dtype = source_array.dtype
    itemsize = dtype.itemsize

    if target_chunks is None:
        # this is just a pass-through copy
        target_chunks = source_chunks

    if isinstance(target_chunks, dict):
        array_dims = _get_dims_from_zarr_array(source_array)
        try:
            target_chunks = _shape_dict_to_tuple(array_dims, target_chunks)
        except KeyError:
            raise KeyError(
                "You must explicitly specify each dimension size in target_chunks. "
                f"Got array_dims {array_dims}, target_chunks {target_chunks}."
            )

    # TODO: rewrite to avoid the hard dependency on dask
    max_mem = dask.utils.parse_bytes(max_mem)

    # don't consolidate reads for Dask arrays
    consolidate_reads = isinstance(source_array, zarr.core.Array)
    read_chunks, int_chunks, write_chunks = rechunking_plan(
        shape,
        source_chunks,
        target_chunks,
        itemsize,
        max_mem,
        consolidate_reads=consolidate_reads,
    )

    # create target
    shape = tuple(int(x) for x in shape)  # ensure python ints for serialization
    target_chunks = tuple(int(x) for x in target_chunks)
    int_chunks = tuple(int(x) for x in int_chunks)
    write_chunks = tuple(int(x) for x in write_chunks)

    target_array = _zarr_empty(
        shape,
        target_store_or_group,
        target_chunks,
        dtype,
        name=name,
        **(target_options or {}),
    )
    try:
        target_array.attrs.update(source_array.attrs)
    except AttributeError:
        pass

    if read_chunks == write_chunks or read_chunks == int_chunks:
        int_array = None
    else:
        # do intermediate store
        if temp_store_or_group is None:
            raise ValueError(
                "A temporary store location must be provided{}.".format(
                    f" (array={name})" if name else ""
                )
            )
        int_array = _zarr_empty(
            shape,
            temp_store_or_group,
            int_chunks,
            dtype,
            name=name,
            **(temp_options or {}),
        )

    read_proxy = ArrayProxy(source_array, read_chunks)
    int_proxy = ArrayProxy(int_array, int_chunks)
    write_proxy = ArrayProxy(target_array, write_chunks)
    return CopySpec(read_proxy, int_proxy, write_proxy)
