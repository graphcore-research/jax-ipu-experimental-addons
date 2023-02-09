// cppimport
// NOTE: comment necessary for automatic JIT compilation of the module!
// Copyright (c) 2022 Graphcore Ltd. All rights reserved.
#define FMT_HEADER_ONLY
#include <fmt/format.h>
#include <fmt/ranges.h>
#include <json/json.hpp>

#include <ipu_custom_primitive.hpp>
#include "tile_array_utils.hpp"

using namespace ipu;

/**
 * @brief Base class for tile put primitives, with common features.
 */
class TilePutBase : public jax::ipu::PrimitiveInterface {
 public:
  /**
   * @brief Extract (and copy) the tile array from raw JSON attributes.
   */
  static std::vector<TileIndexType> extractTileArray(
      const std::string& attributes) {
    return ipu::from_json_str<std::vector<TileIndexType>>(attributes);
  }
};

/**
 * @brief IPU tile put sharded primitive: sharding an array over tiles on
 * the first axis.
 */
class TilePutShardedPrimitive : public TilePutBase {
 public:
  static jax::ipu::PrimitiveMetadata metadata(std::uint32_t num_inputs) {
    return jax::ipu::PrimitiveMetadata{.num_inputs = num_inputs,
                                       .is_elementwise = true,
                                       .is_stateless = true,
                                       .is_hashable = true,
                                       .input_to_output_tensor_aliasing = {{}},
                                       .allocating_indices = {}};
  }

  static poplar::program::Program program(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs,
      std::vector<poplar::Tensor>& outputs, const std::string& attributes,
      const std::string& debug_prefix) {
    const auto debugContext = poplar::DebugContext(debug_prefix);
    if (inputs.size() != 1) {
      throw poputil::poplibs_error(
          "IPU tile put sharded expecting a single input tensor.");
    }
    static_assert(sizeof(TileIndexType) == 4);
    auto input = inputs[0];

    // Passing the tile array as attributes.
    const auto tile_array = extractTileArray(attributes);
    if (input.shape()[0] != tile_array.size()) {
      throw poputil::poplibs_error(
          fmt::format("IPU tile put sharding: inconsistent input size {} and "
                      "tiles length {}.",
                      input.shape()[0], tile_array.size()));
    }

    // Create output tensor, with proper tile mapping.
    // TODO: link to Slack discussion on VarRegion contiguity.
    auto output = createShardedVariable(graph, input.elementType(),
                                        input[0].shape(), tile_array);
    // Copy data tensor into the output.
    auto prog = poplar::program::Copy(input, output);
    outputs.push_back(output);
    return prog;
  }
};

/**
 * @brief IPU tile put replicated primitive: replicating an array over tiles on
 * the first axis.
 */
class TilePutReplicatedPrimitive : public TilePutBase {
 public:
  using TileIndexType = int32_t;

  static jax::ipu::PrimitiveMetadata metadata(std::uint32_t num_inputs) {
    return jax::ipu::PrimitiveMetadata{
        .num_inputs = num_inputs,
        .is_elementwise = false,  // Broadcasting over the first axis.
        .is_stateless = true,
        .is_hashable = true,
        .input_to_output_tensor_aliasing = {{}},
        .allocating_indices = {}};
  }

  static poplar::program::Program program(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs,
      std::vector<poplar::Tensor>& outputs, const std::string& attributes,
      const std::string& debug_prefix) {
    const auto debugContext = poplar::DebugContext(debug_prefix);
    if (inputs.size() != 1) {
      throw poputil::poplibs_error(
          "IPU tile put replicated expecting a single input tensor.");
    }
    static_assert(sizeof(TileIndexType) == 4);
    auto input = inputs[0];

    const auto tile_array = extractTileArray(attributes);
    // Create output tensor, with proper tile mapping.
    auto input_broadcasted = input.expand({0}).broadcast(tile_array.size(), 0);
    auto output = createShardedVariable(graph, input.elementType(),
                                        input.shape(), tile_array);
    // Copy data tensor into the output.
    auto prog = poplar::program::Copy(input_broadcasted, output, false);
    outputs.push_back(output);
    return prog;
  }
};

/**
 * @brief IPU tile gather op parameters.
 */
struct TileGatherParams {
  using TileIndexType = int32_t;
  using TileArrayType = std::vector<TileIndexType>;

  /** Previous input tile mapping (if existing). */
  TileArrayType previous_tiles;
  /** Gather indices. */
  TileArrayType indices;
  /** New tile mapping */
  TileArrayType tiles;
};
NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE(TileGatherParams, previous_tiles, indices,
                                   tiles)
/**
 * @brief IPU tile array (general) gather op across tiles.
 */
class TileGatherPrimitive : public jax::ipu::PrimitiveInterface {
 public:
  using TileIndexType = TileGatherParams::TileIndexType;

  static jax::ipu::PrimitiveMetadata metadata(std::uint32_t num_inputs) {
    return jax::ipu::PrimitiveMetadata{
        .num_inputs = num_inputs,
        .is_elementwise = true,  // Broadcasting over the first axis.
        .is_stateless = true,
        .is_hashable = true,
        .input_to_output_tensor_aliasing = {{}},
        .allocating_indices = {}};
  }

  static poplar::program::Program program(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs,
      std::vector<poplar::Tensor>& outputs, const std::string& attributes,
      const std::string& debug_prefix) {
    const auto debug_context = poplar::DebugContext(debug_prefix);
    if (inputs.size() != 1) {
      throw poputil::poplibs_error(
          "IPU tile gather expecting a single input tensor.");
    }
    const auto& input = inputs[0];
    const auto item_shape = input[0].shape();
    const auto item_type = input.elementType();

    // Tile gather parameters.
    const auto params = ipu::from_json_str<TileGatherParams>(attributes);
    // Create the output tensor per gather index, then concat.
    auto seq = poplar::program::Sequence();
    std::vector<poplar::Tensor> output_slices;
    for (std::size_t idx = 0; idx < params.tiles.size(); ++idx) {
      const auto gather_idx = params.indices[idx];
      // Get the proper item at the gather index.
      const auto input_item = input[gather_idx];
      const auto input_tile = params.previous_tiles[gather_idx];
      const auto output_tile = params.tiles[idx];
      if (input_tile == output_tile)
      {
        // No copy => using directly the existing data on the tile.
        output_slices.push_back(input_item.expand({0}));
      }
      else
      {
        // New Poplar tensor + copy to the proper tile.
        auto output_item = graph.addVariable(item_type, item_shape, debug_context);
        graph.setTileMapping(output_item, output_tile);
        seq.add(poplar::program::Copy(input_item, output_item));
        output_slices.push_back(output_item.expand({0}));
      }
    }
    auto output = poplar::concat(output_slices);
    outputs.push_back(output);
    return seq;
  }
};

/**
 * @brief Tile data Poplar barrier parameters.
 */
struct TileDataBarrierParams {
  using TileIndexType = int32_t;
  using TileArrayType = std::vector<TileIndexType>;

  /** Vertex name to use. */
  std::string vname;
  /** Input tensors tiles. */
  std::vector<TileArrayType> inputs_tiles;
  /** Max tile index used by inputs. */
  TileIndexType max_tile;
};
NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE(TileDataBarrierParams, vname, inputs_tiles,
                                   max_tile)

/**
 * @brief Reinterpret tensor to a reference type used in the tile data barrier.
 */
poplar::Tensor tileBarrierReinterpretTensor(const poplar::Tensor& t) {
  // 8 bits data types.
  if (t.elementType() == poplar::BOOL)
    return t.reinterpret(poplar::UNSIGNED_CHAR);
  else if (t.elementType() == poplar::CHAR)
    return t.reinterpret(poplar::UNSIGNED_CHAR);
  else if (t.elementType() == poplar::SIGNED_CHAR)
    return t.reinterpret(poplar::UNSIGNED_CHAR);
  else if (t.elementType() == poplar::UNSIGNED_CHAR)
    return t.reinterpret(poplar::UNSIGNED_CHAR);
  // 16 bits data types.
  else if (t.elementType() == poplar::SHORT)
    return t.reinterpret(poplar::UNSIGNED_SHORT);
  else if (t.elementType() == poplar::UNSIGNED_SHORT)
    return t.reinterpret(poplar::UNSIGNED_SHORT);
  else if (t.elementType() == poplar::HALF)
    return t.reinterpret(poplar::UNSIGNED_SHORT);
  // 32 bits data types.
  else if (t.elementType() == poplar::INT)
    return t.reinterpret(poplar::UNSIGNED_INT);
  else if (t.elementType() == poplar::UNSIGNED_INT)
    return t.reinterpret(poplar::UNSIGNED_INT);
  else if (t.elementType() == poplar::FLOAT)
    return t.reinterpret(poplar::UNSIGNED_INT);
  // Can handle tensor :/
  throw std::runtime_error("Unknown Poplar tensor type in tile data barrier.");
}

/**
 * @brief IPU tile array data barrier: force to introduce a barrier in Poplar
 * with a single compute set across tiles.
 */
class TileDataBarrierPrimitive : public jax::ipu::PrimitiveInterface {
 public:
  using TileIndexType = TileDataBarrierParams::TileIndexType;

  static jax::ipu::PrimitiveMetadata metadata(std::uint32_t num_inputs) {
    // TODO: input/output aliasing.
    return jax::ipu::PrimitiveMetadata{
        .num_inputs = num_inputs,
        .is_elementwise = false,  // Broadcasting over the first axis.
        .is_stateless = true,
        .is_hashable = true,
        .input_to_output_tensor_aliasing = {{}},
        .allocating_indices = {}};
  }

  static poplar::program::Program program(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs,
      std::vector<poplar::Tensor>& outputs, const std::string& attributes,
      const std::string& debug_prefix) {
    const auto debug_context = poplar::DebugContext(debug_prefix);
    if (inputs.size() < 1) {
      throw poputil::poplibs_error(
          "IPU tile data barrier expecting multiple input tensors.");
    }
    // Tile barrier parameters (with tile sharding).
    const auto params = ipu::from_json_str<TileDataBarrierParams>(attributes);

    // Association of barrier tensors per tile.
    std::vector<std::vector<poplar::Tensor>> tensors_per_tiles(params.max_tile +
                                                               1);
    for (size_t idx = 0; idx < inputs.size(); ++idx) {
      // Reinterpret input tensor to a reference type.
      const auto& in_reinterpret = tileBarrierReinterpretTensor(inputs[idx]);
      const auto& tiles = params.inputs_tiles[idx];
      for (size_t k = 0; k < tiles.size(); ++k) {
        tensors_per_tiles[tiles[k]].push_back(in_reinterpret[k]);
      }
    }

    auto prog = poplar::program::Sequence();
    poplar::ComputeSet cs = graph.addComputeSet(debug_context);
    for (TileIndexType tile = 0; tile < TileIndexType(tensors_per_tiles.size());
         ++tile) {
      const auto& tensors = tensors_per_tiles[tile];
      if (tensors.size() == 0) {
        continue;
      }
      // Add barrier vertex on the tile.
      auto v = graph.addVertex(cs, params.vname);
      graph.setTileMapping(v, tile);
      graph.setPerfEstimate(v, 14);

      // Map collection of tensors to vertex IO.
      graph.connect(v["data"], tensors);
    }
    prog.add(poplar::program::Execute(cs, debug_context));
    outputs = inputs;
    return prog;
  }
};

// Export the IPU JAX primitives in the shared library.
EXPORT_IPU_JAX_PRIMITIVE(TilePutShardedPrimitive);
EXPORT_IPU_JAX_PRIMITIVE(TilePutReplicatedPrimitive);
EXPORT_IPU_JAX_PRIMITIVE(TileGatherPrimitive);
EXPORT_IPU_JAX_PRIMITIVE(TileDataBarrierPrimitive);

// Declare a pybind11, to provide easy compilation & import from Python.
PYBIND11_MODULE(tile_array_primitives_impl, m) {
  using TileIndexType = TileGatherParams::TileIndexType;
  using TileArrayType = TileGatherParams::TileArrayType;

  pybind11::class_<TileGatherParams>(m, "TileGatherParams")
      .def(pybind11::init<>())
      .def(pybind11::init<const TileArrayType&, const TileArrayType&,
                          const TileArrayType&>(),
           pybind11::arg("previous_tiles"), pybind11::arg("indices"),
           pybind11::arg("tiles"))
      .def("to_json_str",
           [](const TileGatherParams& v) { return to_json_str(v); })
      .def_static("from_json_str",
                  [](const std::string& j) {
                    return from_json_str<TileGatherParams>(j);
                  })
      .def_readwrite("previous_tiles", &TileGatherParams::previous_tiles)
      .def_readwrite("indices", &TileGatherParams::indices)
      .def_readwrite("tiles", &TileGatherParams::tiles);

  pybind11::class_<TileDataBarrierParams>(m, "TileDataBarrierParams")
      .def(pybind11::init<>())
      .def(pybind11::init<
               const std::string&,
               const std::vector<TileArrayType>&,
               TileIndexType>(),
           pybind11::arg("vname"), pybind11::arg("inputs_tiles"),
           pybind11::arg("max_tile"))
      .def("to_json_str",
           [](const TileDataBarrierParams& v) { return to_json_str(v); })
      .def_static("from_json_str",
                  [](const std::string& j) {
                    return from_json_str<TileDataBarrierParams>(j);
                  })
      .def_readwrite("vname", &TileDataBarrierParams::vname)
      .def_readwrite("inputs_tiles", &TileDataBarrierParams::inputs_tiles)
      .def_readwrite("max_tile", &TileDataBarrierParams::max_tile);

  pybind11::class_<TilePutShardedPrimitive>(m, "TilePutShardedPrimitive")
      .def_static("metadata", &TilePutShardedPrimitive::metadata,
                  pybind11::arg("num_inputs"));
  pybind11::class_<TilePutReplicatedPrimitive>(m, "TilePutReplicatedPrimitive")
      .def_static("metadata", &TilePutReplicatedPrimitive::metadata,
                  pybind11::arg("num_inputs"));
  pybind11::class_<TileGatherPrimitive>(m, "TileGatherPrimitive")
      .def_static("metadata", &TileGatherPrimitive::metadata,
                  pybind11::arg("num_inputs"));
  pybind11::class_<TileDataBarrierPrimitive>(m, "TileDataBarrierPrimitive")
      .def_static("metadata", &TileDataBarrierPrimitive::metadata,
                  pybind11::arg("num_inputs"));
}

// cppimport configuration for compiling the pybind11 module.
// clang-format off
/*
<%
cfg['extra_compile_args'] = ['-std=c++17', '-fPIC', '-O2', '-Wall']
cfg['libraries'] = ['poplar', 'poputil']
cfg['include_dirs'] = []
setup_pybind11(cfg)
%>
*/
