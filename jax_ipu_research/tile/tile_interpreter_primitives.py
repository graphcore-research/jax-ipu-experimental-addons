# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
import base64
import os
from copy import copy
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import cppimport
import numpy as np
from jax import core, vmap
from jax.interpreters import mlir, xla
from jax.interpreters.mlir import LoweringRuleContext, ir
from jax.interpreters.xla import ShapedArray
from jax.lib import xla_client
from jax_ipu_addons.primitives.custom_primitive_utils import ipu_xla_custom_primitive_call

from jax_ipu_research.utils import Array

from .tile_array_primitives import Base64Data, IpuType
from .tile_common_utils import from_ipu_type_to_numpy_dtype, from_numpy_dtype_to_ipu_type, get_ipu_type_name

# Pybind11 extension import (and compilation if necessary).
# Explicit path is more robust to different `pip install` usages.
ext_filename = os.path.abspath(os.path.join(os.path.dirname(__file__), "tile_interpreter_primitives_impl.cpp"))
tile_interpreter_primitives_impl = cppimport.imp_from_filepath(
    ext_filename, "jax_ipu_research.tile.tile_interpreter_primitives_impl"
)

from .tile_interpreter_primitives_impl import (  # noqa: E402
    IpuTileMapEquation,
    IpuVertexAttributeF32,
    IpuVertexAttributeI32,
    IpuVertexIOInfo,
    IpuVertexIOType,
    TileMapEquationCall,
)


def make_ipu_vertex_name_templated(basename: str, *args) -> str:
    """Make an IPU vertex full/templated name, from a basename and additional arguments."""

    def get_arg_name(v) -> str:
        if isinstance(v, str):
            return v
        elif isinstance(v, (bool, int)):
            return str(v).lower()
        elif isinstance(v, (IpuType, np.dtype)) or (isinstance(v, type) and issubclass(v, np.number)):
            return get_ipu_type_name(v)
        raise ValueError(f"Unknown IPU template argument type: {v}.")

    if len(args) == 0:
        return basename

    args_name = ",".join([get_arg_name(v) for v in args])
    return f"{basename}<{args_name}>"


def make_ipu_vertex_io_info(
    name: str, iotype: IpuVertexIOType, aval: ShapedArray, vertex_dim2: int = 0
) -> IpuVertexIOInfo:
    """Make IPU vertex IO info.

    Args:
        name: IO field name.
        iotype: IO type.
        aval: Shaped array.
        vertex_dim2: Vertex IO tensor 2nd dimension.
    Returns:
        IPU vertex IO info.
    """
    ipu_type = from_numpy_dtype_to_ipu_type(aval.dtype)
    return IpuVertexIOInfo(name=name, iotype=iotype, shape=aval.shape, dtype=ipu_type, vertex_dim2=int(vertex_dim2))


def make_ipu_vertex_constant_info(name: str, data: np.ndarray, vertex_dim2: int = 0) -> IpuVertexIOInfo:
    """Make IPU vertex constant input info.

    Args:
        name: IO field name.
        data: Numpy array with the constant data.
        vertex_dim2: Vertex IO tensor 2nd dimension.
    Returns:
        IPU vertex IO info.
    """
    data = np.asarray(data)
    ipu_type = from_numpy_dtype_to_ipu_type(data.dtype)
    constant_data = Base64Data(base64.b64encode(data))
    return IpuVertexIOInfo(
        name=name,
        iotype=IpuVertexIOType.In,
        shape=data.shape,
        dtype=ipu_type,
        vertex_dim2=vertex_dim2,
        constant_data=constant_data,
    )


def make_ipu_vertex_in_info(name: str, aval: ShapedArray, vertex_dim2: int = 0) -> IpuVertexIOInfo:
    """Make IPU vertex IN (input) info."""
    return make_ipu_vertex_io_info(name, IpuVertexIOType.In, aval, vertex_dim2)


def make_ipu_vertex_out_info(name: str, aval: ShapedArray, vertex_dim2: int = 0) -> IpuVertexIOInfo:
    """Make IPU vertex OUT (output) info."""
    return make_ipu_vertex_io_info(name, IpuVertexIOType.Out, aval, vertex_dim2)


def make_ipu_vertex_inout_info(name: str, aval: ShapedArray, vertex_dim2: int = 0) -> IpuVertexIOInfo:
    """Make IPU vertex IN-OUT (input-output) info."""
    return make_ipu_vertex_io_info(name, IpuVertexIOType.InOut, aval, vertex_dim2)


def make_ipu_vertex_inputs(
    inavals: Dict[str, ShapedArray], inout_names: Set[str] = set(), vertex_dims2: Dict[str, int] = dict()
) -> List[IpuVertexIOInfo]:
    """Build a collection of IPU vertex input infos.

    Args:
        inavals: Named collection of input avals.
        inout_names: Name of tensors with InOut status.
        vertex_dims2: Name of tensors with second vertex dim.
    Returns:
        List of IPU vertex IO info.
    """

    def _get_iotype(name: str):
        return IpuVertexIOType.InOut if name in inout_names else IpuVertexIOType.In

    def _get_vertex_dim2(name: str):
        return vertex_dims2.get(name, 0)

    return [
        make_ipu_vertex_io_info(name, _get_iotype(name), aval=aval, vertex_dim2=_get_vertex_dim2(name))
        for name, aval in inavals.items()
    ]


def make_ipu_vertex_outputs(
    outavals: Dict[str, ShapedArray], inout_names: Set[str] = set(), vertex_dims2: Dict[str, int] = dict()
) -> List[IpuVertexIOInfo]:
    """Build a collection of IPU vertex output infos.

    Args:
        inavals: Named collection of output avals.
        inout_names: Name of tensors with InOut status.
        vertex_dims2: Name of tensors with second vertex dim.
    Returns:
        List of IPU vertex IO info.
    """

    def _get_iotype(name: str):
        return IpuVertexIOType.InOut if name in inout_names else IpuVertexIOType.Out

    def _get_vertex_dim2(name: str):
        return vertex_dims2.get(name, 0)

    return [
        make_ipu_vertex_io_info(name, _get_iotype(name), aval=aval, vertex_dim2=_get_vertex_dim2(name))
        for name, aval in outavals.items()
    ]


def make_ipu_vertex_attributes(**kwargs) -> Tuple[List[IpuVertexAttributeI32], List[IpuVertexAttributeF32]]:
    """Make IPU vertex attributes, uint32 or floating.

    Args:
        kwargs: Named attributes.
    Returns:
        Int32 and floating attributes.
    """
    attrs_i32: List[IpuVertexAttributeI32] = []
    attrs_f32: List[IpuVertexAttributeF32] = []
    for k, v in kwargs.items():
        if isinstance(v, (int, np.int32, np.int64)):
            attrs_i32.append(IpuVertexAttributeI32(k, int(v)))
        elif isinstance(v, (float, np.float32, np.float64)):
            attrs_f32.append(IpuVertexAttributeF32(k, v))
        else:
            raise TypeError(f"Unknown IPU vertex attribute type {k}: {v} with type {type(v)}.")
    return attrs_i32, attrs_f32


def tile_map_remove_ipu_attributes(attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Remove IPU attributes from a dictionary."""
    ipu_prefix = "ipu_"
    return {k: v for k, v in attributes.items() if not k.startswith(ipu_prefix)}


def get_tile_map_ipu_arguments(**kwargs) -> Tuple[str, Tuple[int, ...], str]:
    """Get the tile map arguments: primitive name, tiles and eqn."""
    return kwargs["pname"], kwargs["tiles"], kwargs["tile_map_eqn_json"]


def get_primitive_arguments(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get the tile map arguments: primitive name, tiles and eqn."""
    params = copy(params)
    params.pop("pname", None)
    params.pop("tiles", None)
    params.pop("tile_map_eqn_json", None)
    params = tile_map_remove_ipu_attributes(params)
    return params


# Two primitives required, to differentiate single/multi output cases.
tile_map_equation_call_single_out_p = core.Primitive("tile_map_equation_call_single_out")
tile_map_equation_call_multi_out_p = core.Primitive("tile_map_equation_call_multi_out")


def tile_map_equation_call_single_out(
    inputs: Sequence[Array], pname: str, tiles: Tuple[int, ...], tile_map_eqn_json: str, **kwargs
) -> Array:
    return tile_map_equation_call_single_out_p.bind(
        *inputs, pname=pname, tiles=tiles, tile_map_eqn_json=tile_map_eqn_json, **kwargs
    )


def tile_map_equation_call_multi_out(
    inputs: Sequence[Array], pname: str, tiles: Tuple[int, ...], tile_map_eqn_json: str, **kwargs
) -> Sequence[Array]:
    return tile_map_equation_call_multi_out_p.bind(
        *inputs, pname=pname, tiles=tiles, tile_map_eqn_json=tile_map_eqn_json, **kwargs
    )


def tile_map_equation_call_impl(*args, **params):
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, _, _ = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)

    def primitive_fn(*args):
        return primitive.bind(*args, **get_primitive_arguments(params))

    # Use `vmap` to run the equivalent computation on any device.
    # TODO: caching of vmap function?
    vmap_primitive_fn = vmap(primitive_fn, in_axes=0, out_axes=0)
    return vmap_primitive_fn(*args)


def tile_map_equation_call_abstract_eval(*args, **params) -> Union[ShapedArray, Tuple[ShapedArray]]:
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, tiles, _ = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)
    num_tiles = len(tiles)
    # Abstract eval at the tile level.
    tile_args = [ShapedArray(v.shape[1:], v.dtype) for v in args]
    tile_outputs = primitive.abstract_eval(*tile_args, **get_primitive_arguments(params))
    # TODO: investigate what the second return value in `abstract_eval`?
    if isinstance(tile_outputs, tuple) and isinstance(tile_outputs[-1], set):
        tile_outputs = tile_outputs[0]
    # Re-construct sharded abtract output
    if not primitive.multiple_results:
        tile_outputs = [tile_outputs]
    outputs = tuple([ShapedArray((num_tiles, *v.shape), v.dtype) for v in tile_outputs])
    if not primitive.multiple_results:
        outputs = outputs[0]
    return outputs


def tile_map_equation_call_mlir_translation_default(
    ctx: LoweringRuleContext, *args: Union[ir.Value, Sequence[ir.Value]], **params
) -> Sequence[Union[ir.Value, Sequence[ir.Value]]]:
    """`tile_map_equation_call` default MLIR translation, for CPU/GPU backends."""
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, _, _ = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)

    # Not sure using a local function is a good idea?
    def primitive_fn(*inputs):
        return primitive.bind(*inputs, **get_primitive_arguments(params))

    # Use `vmap` to run the equivalent computation on any device.
    vmap_primitive_fn = vmap(primitive_fn, in_axes=0, out_axes=0)
    # Lower to MLIR using JAX tooling.
    # TODO: cache lowering?
    vmap_primitive_lower_fn = mlir.lower_fun(vmap_primitive_fn, multiple_results=primitive.multiple_results)
    return vmap_primitive_lower_fn(ctx, *args)


def tile_map_equation_call_xla_translation_ipu(ctx, *xla_args, **params):
    """`tile_map_equation_call` IPU backend XLA translation, as a custom primitive.

    TODO: Move to MLIR translation.

    Args:
        ctx: XLA context
        args: XLA operands
        params: Additiona parameters/attributes to pass.
    """
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, tiles, tile_map_eqn_json = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)
    num_tiles = len(tiles)
    # Tile map equation (serialized as json).
    tile_map_eqn = IpuTileMapEquation.from_json_str(tile_map_eqn_json)
    # Outputs (sharded) abstract values.
    outputs_aval = [
        ShapedArray((num_tiles, *v.shape), from_ipu_type_to_numpy_dtype(v.dtype)) for v in tile_map_eqn.outputs_info
    ]
    # Load optional vertex compiled file (or cpp)
    ipu_gp_filename: Optional[str] = None
    if len(tile_map_eqn.gp_filename) > 0:
        ipu_gp_filename = os.path.abspath(tile_map_eqn.gp_filename)
    outputs = ipu_xla_custom_primitive_call(
        TileMapEquationCall,
        ctx,
        xla_args,
        outputs_aval,
        attributes=tile_map_eqn_json,
        ipu_gp_filename=ipu_gp_filename,
    )
    if not primitive.multiple_results:
        return outputs[0]
    # Re-construct the XLA tuple (TODO: clean this back & forth mess!)
    return xla_client.ops.Tuple(ctx, outputs)


tile_map_equation_call_single_out_p.multiple_results = False
tile_map_equation_call_multi_out_p.multiple_results = True
# Register the primal implementation with JAX
tile_map_equation_call_single_out_p.def_impl(tile_map_equation_call_impl)
tile_map_equation_call_multi_out_p.def_impl(tile_map_equation_call_impl)
# Register the abstract evaluation with JAX
tile_map_equation_call_single_out_p.def_abstract_eval(tile_map_equation_call_abstract_eval)
tile_map_equation_call_multi_out_p.def_abstract_eval(tile_map_equation_call_abstract_eval)

# Register IPU XLA translation. TODO: move to MLIR.
xla.backend_specific_translations["ipu"][
    tile_map_equation_call_single_out_p
] = tile_map_equation_call_xla_translation_ipu
xla.backend_specific_translations["ipu"][
    tile_map_equation_call_multi_out_p
] = tile_map_equation_call_xla_translation_ipu

# Register MLIR translation for other backends.
other_backends = ["cpu", "cuda", "tpu", "rocm"]
for b in other_backends:
    mlir.register_lowering(tile_map_equation_call_single_out_p, tile_map_equation_call_mlir_translation_default, b)
    mlir.register_lowering(tile_map_equation_call_multi_out_p, tile_map_equation_call_mlir_translation_default, b)
