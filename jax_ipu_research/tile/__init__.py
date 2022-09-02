# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
# Register basic JAX primitives for tile interpreter.
from . import external_libs, tile_interpreter_lax_binary, tile_interpreter_lax_unary
from .tile_array import TileShardedArray, tile_put_replicated, tile_put_sharded
from .tile_interpreter import register_ipu_tile_primitive, tile_map_primitive
