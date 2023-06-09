# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
from typing import Any, Dict, List, Tuple

import numpy as np
from jax import lax
from jax.core import Primitive, ShapedArray

from jax_ipu_experimental_addons.utils import DTypeLike

from .tile_interpreter import register_ipu_tile_primitive
from .tile_interpreter_primitives import (
    IpuTileMapEquation,
    get_ipu_type_name,
    make_ipu_vertex_attributes,
    make_ipu_vertex_in_info,
    make_ipu_vertex_inout_info,
    make_ipu_vertex_out_info,
)

# popops definitions.
# enum class TernaryOpType { CLAMP, SELECT };

# enum class BinaryOpType {
#   ADD,
#   ATAN2,
#   BITWISE_AND,
#   BITWISE_OR,
#   BITWISE_XOR,
#   BITWISE_XNOR,
#   DIVIDE,
#   EQUAL,
#   GREATER_THAN_EQUAL,
#   GREATER_THAN,
#   INV_STD_DEV_TO_VARIANCE,
#   LESS_THAN_EQUAL,
#   LOGICAL_AND,
#   LOGICAL_OR,
#   LESS_THAN,
#   MAXIMUM,
#   MINIMUM,
#   MULTIPLY,
#   NOT_EQUAL,
#   POWER,
#   REMAINDER,
#   SHIFT_LEFT,
#   SHIFT_RIGHT,
#   SHIFT_RIGHT_SIGN_EXTEND,
#   SUBTRACT,
#   VARIANCE_TO_INV_STD_DEV
# };

_binary_primitive_to_vertex_basename: Dict[Primitive, Tuple[str, Any]] = {
    lax.add_p: ("ADD", None),
    lax.atan2_p: ("ATAN2", None),
    lax.div_p: ("DIVIDE", None),
    lax.eq_p: ("EQUAL", np.bool_),
    lax.ge_p: ("GREATER_THAN_EQUAL", np.bool_),
    lax.gt_p: ("GREATER_THAN", np.bool_),
    lax.le_p: ("LESS_THAN_EQUAL", np.bool_),
    lax.lt_p: ("LESS_THAN", np.bool_),
    lax.max_p: ("MAXIMUM", None),
    lax.min_p: ("MINIMUM", None),
    lax.mul_p: ("MULTIPLY", None),
    lax.ne_p: ("NOT_EQUAL", np.bool_),
    lax.pow_p: ("POWER", None),
    lax.rem_p: ("REMAINDER", None),
    lax.sub_p: ("SUBTRACT", None),
    # NOTE: Poplar (SDK 3.1) having slightly different convention than XLA/JAX.
    lax.shift_left_p: ("SHIFT_LEFT", None),
    lax.shift_right_logical_p: ("SHIFT_RIGHT", None),
    lax.shift_right_arithmetic_p: ("SHIFT_RIGHT_SIGN_EXTEND", None),
}
"""Binary JAX primitive to to IPU vertex basename (and output optional dtype).
"""


def make_binary1d_vertex_fullname(basename: str, dtype: DTypeLike) -> str:
    """Create the full vertex name from the basename and dtype."""
    ipu_dtype = get_ipu_type_name(dtype)
    return f"popops::BinaryOp1D<popops::expr::BinaryOpType::{basename},{ipu_dtype}>"


def ipu_binary_primitive_translation(
    p: Primitive,
    tiles: Tuple[int, ...],
    inavals: List[ShapedArray],
    attributes: Dict[str, Any] = None,
) -> IpuTileMapEquation:
    """IPU binary primitive translation rule to IPU vertex.

    Args:
        p: JAX primitive.
        tiles: Collection of tiles.
        inavals: Input shaped arrays.
        attributes: (unused) attributes.
    Returns:
        IPU tile map primitive structure.
    """
    assert len(inavals) == 2
    vertex_basename, outdtype = _binary_primitive_to_vertex_basename[p]
    vname = make_binary1d_vertex_fullname(vertex_basename, inavals[0].dtype)
    outaval = ShapedArray(inavals[0].shape, outdtype or inavals[0].dtype)
    ipu_prim_info = IpuTileMapEquation(
        vname=vname,
        pname=p.name,
        tiles=tiles,
        inputs_info=[
            make_ipu_vertex_in_info("in1", inavals[0]),
            make_ipu_vertex_in_info("in2", inavals[1]),
        ],
        outputs_info=[make_ipu_vertex_out_info("out", outaval)],
        attributes_i32=[],
        attributes_f32=[],
    )
    return ipu_prim_info


# Register all supported JAX unary ops.
for p in _binary_primitive_to_vertex_basename.keys():
    register_ipu_tile_primitive(p, ipu_binary_primitive_translation)


scaled_add_p = Primitive("scaled_add")
scaled_sub_p = Primitive("scaled_sub")


def scaled_add(a, b, sb):
    return scaled_add_p.bind(a, b, sb)


def scaled_sub(a, b, sb):
    return scaled_sub_p.bind(a, b, sb)


def scaled_add_numpy_impl(a, b, sb):
    return a + sb * b


def scaled_sub_numpy_impl(a, b, sb):
    return a - sb * b


def scaled_op_abstract_eval(a, b, sb):
    return a


def make_scale_op_vertex_fullname(basename: str, dtype: DTypeLike) -> str:
    """Create the full vertex name from the basename and dtype."""
    mem_constraints = False
    mem_constraints_str = str(mem_constraints).lower()
    ipu_dtype = get_ipu_type_name(dtype)
    return f"popops::{basename}<{ipu_dtype},{ipu_dtype},{ipu_dtype},{mem_constraints_str}>"


def scale_op_tile_translation_ipu(
    p: Primitive,
    vertex_name: str,
    tiles: Tuple[int, ...],
    inavals: List[ShapedArray],
    attributes: Dict[str, Any] = None,
) -> IpuTileMapEquation:
    """IPU tile translation for scaled op.

    Args:
        p: JAX primitive.
        tiles: Collection of tiles.
        inavals: Input shaped arrays.
        attributes: Op attributes.
    Returns:
        IPU tile map primitive structure.
    """
    assert len(inavals) == 3
    aaval, baval, sbaval = inavals
    assert aaval.shape == baval.shape
    assert aaval.dtype == baval.dtype
    assert sbaval.size == 1

    # Translation rule to IPU vertex
    attrs_i32, attrs_f32 = make_ipu_vertex_attributes(size=aaval.size)
    ipu_prim_info = IpuTileMapEquation(
        vname=vertex_name,
        pname=p.name,
        tiles=tiles,
        # IO vertex infos.
        inputs_info=[
            make_ipu_vertex_inout_info("A", aaval),
            make_ipu_vertex_in_info("B", baval),
            make_ipu_vertex_in_info("scaleB", sbaval),
        ],
        outputs_info=[make_ipu_vertex_inout_info("A", aaval)],
        attributes_i32=attrs_i32,
        attributes_f32=attrs_f32,
        # Perf. estimate from Poplar code.
        # perf_estimate=aaval.size * 2,
    )
    return ipu_prim_info


def scale_add_tile_translation_ipu(
    p: Primitive,
    tiles: Tuple[int, ...],
    inavals: List[ShapedArray],
    attributes: Dict[str, Any] = None,
) -> IpuTileMapEquation:
    assert len(inavals) == 3
    vertex_name = make_scale_op_vertex_fullname("ScaledAddSupervisor", inavals[0].dtype)
    return scale_op_tile_translation_ipu(p, vertex_name, tiles, inavals, attributes)


def scale_sub_tile_translation_ipu(
    p: Primitive,
    tiles: Tuple[int, ...],
    inavals: List[ShapedArray],
    attributes: Dict[str, Any] = None,
) -> IpuTileMapEquation:
    assert len(inavals) == 3
    vertex_name = make_scale_op_vertex_fullname("ScaledSubtractSupervisor", inavals[0].dtype)
    return scale_op_tile_translation_ipu(p, vertex_name, tiles, inavals, attributes)


scaled_add_p.map_primitive = False
scaled_sub_p.map_primitive = False
# Register the primal implementation with JAX
scaled_add_p.def_impl(scaled_add_numpy_impl)
scaled_sub_p.def_impl(scaled_sub_numpy_impl)
# Register the abstract evaluation with JAX
scaled_add_p.def_abstract_eval(scaled_op_abstract_eval)
scaled_sub_p.def_abstract_eval(scaled_op_abstract_eval)
# Register tile IPU translation.
register_ipu_tile_primitive(scaled_add_p, scale_add_tile_translation_ipu)
register_ipu_tile_primitive(scaled_sub_p, scale_sub_tile_translation_ipu)
