# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
import os
from copy import copy
from typing import Any, Dict, List, Optional, Tuple

import cppimport.import_hook  # noqa: F401
import numpy as np
from jax import core
from jax.interpreters import xla
from jax.interpreters.xla import ShapedArray
from jax_ipu_addons.primitives.custom_primitive_utils import ipu_xla_custom_primitive_call

from .tile_interpreter_primitives_impl import (
    IpuTileMapEquation,
    IpuType,
    IpuVertexIOInfo,
    IpuVertexIOType,
    TileMapEquationCall,
)

_numpy_dtype_to_ipu_type = {
    np.dtype(np.bool_): IpuType.BOOL,
    np.dtype(np.int8): IpuType.CHAR,
    np.dtype(np.int16): IpuType.SHORT,
    np.dtype(np.int32): IpuType.INT,
    np.dtype(np.float16): IpuType.HALF,
    np.dtype(np.float32): IpuType.FLOAT,
}
"""Mapping from Numpy dtype to IPU datatype.
"""

_ipu_type_to_numpy_dtype = {
    IpuType.BOOL: np.dtype(np.bool_),
    IpuType.CHAR: np.dtype(np.int8),
    IpuType.SHORT: np.dtype(np.int16),
    IpuType.INT: np.dtype(np.int32),
    IpuType.HALF: np.dtype(np.float16),
    IpuType.FLOAT: np.dtype(np.float32),
}
"""Mapping from IPU type to Numpy dtype.
"""


def from_numpy_dtype_to_ipu_type(v: Any) -> IpuType:
    """Convert from Numpy dtype to IPU type."""
    return _numpy_dtype_to_ipu_type[np.dtype(v)]


def from_ipu_type_to_numpy_dtype(v: IpuType) -> Any:
    """Convert from IPU type to Numpy dtype."""
    return _ipu_type_to_numpy_dtype[v]


def make_ipu_vertex_io_info(name: str, iotype: IpuVertexIOType, aval: ShapedArray, rank: int = 1) -> IpuVertexIOInfo:
    """Make IPU vertex IO info.

    Args:
        name: IO field name.
        iotype: IO type.
        aval: Shaped array.
        rank: Vertex IO tensor rank (1 or 2 supported).
    Returns:
        IPU vertex IO info.
    """
    ipu_type = from_numpy_dtype_to_ipu_type(aval.dtype)
    return IpuVertexIOInfo(name=name, iotype=iotype, shape=aval.shape, dtype=ipu_type, rank=rank)


def get_tile_map_ipu_arguments(**kwargs) -> Tuple[str, Tuple[int, ...], str]:
    """Get the tile map arguments: primitive name, tiles and eqn."""
    return kwargs["pname"], kwargs["tiles"], kwargs["tile_map_eqn_json"]


def get_primitive_arguments(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get the tile map arguments: primitive name, tiles and eqn."""
    params = copy(params)
    params.pop("pname", None)
    params.pop("tiles", None)
    params.pop("tile_map_eqn_json", None)
    return params


tile_map_equation_call_p = core.Primitive("tile_map_equation_call")


def tile_map_equation_call(inputs, pname: str, tiles: Tuple[int, ...], tile_map_eqn_json: str, **kwargs):
    return tile_map_equation_call_p.bind(
        *inputs, pname=pname, tiles=tiles, tile_map_eqn_json=tile_map_eqn_json, **kwargs
    )


def tile_map_equation_call_impl(*args, **params):
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, _, _ = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)
    # TODO: should use `vmap` for proper mapping.
    outputs = primitive.bind(*args, **get_primitive_arguments(params))
    return outputs


def tile_map_equation_call_abstract_eval(*args, **params) -> List[ShapedArray]:
    from .tile_interpreter import get_ipu_tile_primitive_translation

    pname, tiles, _ = get_tile_map_ipu_arguments(**params)
    primitive, _ = get_ipu_tile_primitive_translation(pname)
    num_tiles = len(tiles)
    # Abstract eval at the tile level.
    tile_args = [ShapedArray(v.shape[1:], v.dtype) for v in args]
    tile_outputs = primitive.abstract_eval(*tile_args, **get_primitive_arguments(params))
    # Re-construct sharded abtract output
    if not primitive.multiple_results:
        tile_outputs = [tile_outputs]
    outputs = [ShapedArray((num_tiles, *v.shape), v.dtype) for v in tile_outputs]
    if not primitive.multiple_results:
        outputs = outputs[0]
    return outputs


def tile_map_equation_call_xla_translation_default(ctx, *xla_args, **params):
    """`tile_map_equation_call` default XLA translation, for CPU/GPU backends."""
    raise NotImplementedError("No CPU/GPU implementation of `tile_map_equation_call`.")


def tile_map_equation_call_xla_translation_ipu(ctx, *xla_args, **params):
    """`tile_map_equation_call` IPU backend XLA translation, as a custom primitive."""
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
        TileMapEquationCall, ctx, xla_args, outputs_aval, attributes=tile_map_eqn_json, ipu_gp_filename=ipu_gp_filename
    )
    if not primitive.multiple_results:
        outputs = outputs[0]
    return outputs


# Register the primal implementation with JAX
tile_map_equation_call_p.def_impl(tile_map_equation_call_impl)
# Register the abstract evaluation with JAX
tile_map_equation_call_p.def_abstract_eval(tile_map_equation_call_abstract_eval)
# Register XLA translation, for different backends.
xla.backend_specific_translations["ipu"][tile_map_equation_call_p] = tile_map_equation_call_xla_translation_ipu
xla.backend_specific_translations["cpu"][tile_map_equation_call_p] = tile_map_equation_call_xla_translation_default
xla.backend_specific_translations["gpu"][tile_map_equation_call_p] = tile_map_equation_call_xla_translation_default
