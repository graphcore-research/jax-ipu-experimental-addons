# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
from functools import partial

import chex
import jax
import numpy as np
import numpy.testing as npt
from absl.testing import parameterized

from jax_ipu_research import is_ipu_model
from jax_ipu_research.tile import TileShardedArray, ipu_hw_cycle_count, tile_put_replicated


class IpuTileHardwarePrimitives(chex.TestCase, parameterized.TestCase):
    def setUp(self):
        self.device = jax.devices("ipu")[0]
        self.num_tiles = self.device.num_tiles

    # @pytest.mark.skipif(is_ipu_model(jax.devices("ipu")[0]), reason="Not supported on IPU model.")
    @parameterized.parameters({"sync": True}, {"sync": False})
    def test__ipu_hw_cycle_count__proper_hw_counter(self, sync: bool):
        tiles = (1, 2, self.num_tiles - 1)
        val = np.random.rand(1).astype(np.float32)

        @partial(jax.jit, backend="ipu")
        def compute_fn(val):
            val = tile_put_replicated(val, tiles)
            val, cycles0 = ipu_hw_cycle_count(val, sync=sync)
            val, cycles1 = ipu_hw_cycle_count(val, sync=False)
            return cycles0, cycles1

        cycles0_ipu, cycles1_ipu = compute_fn(val)

        assert isinstance(cycles0_ipu, TileShardedArray)
        assert cycles0_ipu.tiles == tiles
        assert cycles0_ipu.dtype == np.uint32
        assert cycles0_ipu.shape == (len(tiles), 2)

        cycles0_ipu = np.asarray(cycles0_ipu)
        cycles1_ipu = np.asarray(cycles1_ipu)
        diff_cycles_count = cycles1_ipu - cycles0_ipu

        if is_ipu_model(jax.devices("ipu")[0]):
            # IPU model: zero cycle count.
            npt.assert_equal(diff_cycles_count, 0)
        else:
            # Real IPU hw.
            npt.assert_equal(diff_cycles_count[:, 1], 0)
            npt.assert_equal(diff_cycles_count[:, 0] > 30, True)
            npt.assert_equal(diff_cycles_count[:, 0] <= 150, True)
            # Should be the same accross all tiles, even without synchronization.
            npt.assert_equal(diff_cycles_count[:, 0], diff_cycles_count[0, 0])
