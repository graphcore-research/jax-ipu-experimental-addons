// cppimport
// NOTE: comment necessary for automatic JIT compilation of the module!
// Copyright (c) 2022 Graphcore Ltd. All rights reserved.
#define FMT_HEADER_ONLY
#include <fmt/format.h>
#include <json/json.hpp>

#include <algorithm>
#include <ipu_custom_primitive.hpp>
#include "tile_array_utils.hpp"

namespace ipu {
using json = nlohmann::json;

/**
 * @brief Vertex IO tensor type.
 */
enum class VertexIOType : int {
  In = 0,    // Input only tensor.
  Out = 1,   // Output only tensor.
  InOut = 2  // Input/output tensor.
};

/**
 * @brief JAX-like shaped array data structure.
 */
struct ShapedArray {
  /** Shape of the array. */
  ShapeType shape;
  /** Dtype of the array. */
  IpuType dtype;
};
NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE(ShapedArray, shape, dtype)

/**
 * @brief Vertex IO tensor info.
 */
struct VertexIOInfo {
  /** Name of the vertex IO tensor. */
  std::string name;
  /** IO tensor iotype. */
  VertexIOType iotype;
  /** IO tensor aval. */
  ShapedArray aval;
  /** IO tensor rank. 1 (by default) or 2 supported. */
  uint8_t rank = 1;

  /**
   * @brief Reshape a tensor to the proper rank for vertex connection.
   */
  poplar::Tensor connectReshape(const poplar::Tensor& t) const {
    if (rank == 1) {
      // Rank 1: flatten the IO tensor.
      return t.flatten();
    } else if (rank == 2) {
      // Assume already of rank 2. Poplar will check.
      return t;
    }
    throw poputil::poplibs_error("IPU IO vertex tensor must of rank 1 or 2.");
  }
};
NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE(VertexIOInfo, name, iotype, aval, rank)

bool operator==(const VertexIOInfo& lhs, const VertexIOInfo& rhs) {
  return lhs.name == rhs.name && lhs.iotype == rhs.iotype &&
         lhs.aval.shape == rhs.aval.shape && lhs.aval.dtype == rhs.aval.dtype &&
         lhs.rank == rhs.rank;
}

/**
 * @brief Vertex (static) attribute
 * @tparam T Attribute type.
 */
template <typename T>
struct VertexAttribute {
  /** Name of the attribute. */
  std::string name;
  /** Value of the attribute. */
  T value;
};

template <typename T>
bool operator==(const VertexAttribute<T>& lhs, const VertexAttribute<T>& rhs) {
  return lhs.name == rhs.name && lhs.value == rhs.value;
}
template <typename T>
void to_json(json& j, const VertexAttribute<T>& v) {
  j = json{{"name", v.name}, {"value", v.value}};
}
template <typename T>
void from_json(const json& j, VertexAttribute<T>& v) {
  j.at("name").get_to(v.name);
  j.at("value").get_to(v.value);
}

/**
 * @brief Make Python bindings of VertexAttribute class.
 */
template <typename T>
decltype(auto) makeVertexAttributeBindings(pybind11::module& m,
                                           const char* name) {
  using VertexAttrType = VertexAttribute<T>;
  pybind11::class_<VertexAttrType>(m, name)
      .def(pybind11::init<>())
      .def(pybind11::init<const std::string&, T>(), pybind11::arg("name"),
           pybind11::arg("value"))
      .def(pybind11::self == pybind11::self)
      .def("to_json_str",
           [](const VertexAttrType& v) { return to_json_str(v); })
      .def_static(
          "from_json_str",
          [](const std::string& j) { return from_json_str<VertexAttrType>(j); })
      .def_readwrite("name", &VertexAttrType::name)
      .def_readwrite("value", &VertexAttrType::value);
}

using VertexAttributeU32 = VertexAttribute<uint32_t>;
using VertexAttributeF32 = VertexAttribute<float>;

/**
 * @brief IPU tile map(ped) equation (on the model of JAX equation).
 *
 * This class represent a tile equation mapped on multiple tiles (which the same
 * input/output shapes, and constant attributes).
 *
 * IPU parallelization between tiles: disjoint compute sets should be executed
 * in parallel:
 *   https://graphcore.slack.com/archives/C013LPHPX61/p1661937739927649
 */
struct TileMapEquation {
  /** Primitive name. */
  std::string pname;
  /** Vertex name. */
  std::string vname;

  /** Tiles on which is the equation is mapped. */
  std::vector<TileIndexType> tiles;

  /** Input vertex tensor infos (per tile). */
  std::vector<VertexIOInfo> inputs_info;
  /** Output vertex tensor infos (per tile). */
  std::vector<VertexIOInfo> outputs_info;

  /** Attributes, with different types. */
  std::vector<VertexAttributeU32> attributes_u32;
  std::vector<VertexAttributeF32> attributes_f32;

  /** (Optional) IPU gp vertex (absolute) filename. */
  std::string gp_filename;
  /** Vertex performance estimate (optional). */
  uint64_t perf_estimate = 0;

  /**
   * @brief Allocate output (or use existing input) tensors.
   * @param graph Poplar graph.
   * @param inputs Corresponding tensor inputs.
   * @return Collection of output tensors.
   */
  std::vector<poplar::Tensor> allocateOutputTensors(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs) const {
    FMT_ASSERT(inputs.size() == inputs_info.size(),
               "Inconsistent input vector size.");

    std::vector<poplar::Tensor> outputs;
    for (const auto& outinfo : outputs_info) {
      if (outinfo.iotype == VertexIOType::InOut) {
        // Find the input tensor used as output.
        const auto it = std::find_if(inputs_info.begin(), inputs_info.end(),
                                     [&outinfo](const VertexIOInfo& ininfo) {
                                       return ininfo.name == outinfo.name;
                                     });
        const auto idx = std::distance(inputs_info.begin(), it);
        outputs.push_back(inputs.at(idx));
      } else if (outinfo.iotype == VertexIOType::Out) {
        // Allocate an output tensor with proper shape.
        outputs.push_back(
            createShardedVariable(graph, toPoplar(outinfo.aval.dtype),
                                  outinfo.aval.shape, this->tiles));
      } else {
        throw std::runtime_error("Unknown IO type for vertex output tensor.");
      }
    }
    return outputs;
  }

  /**
   * @brief Add vertex/equation to Poplar graph & compute set.
   *
   * @param graph Poplar graph.
   * @param prog Poplar sequence program.
   * @param inputs Vector of (sharded) input tensors.
   * @param outputs Vector of (sharded) output tensors (already allocated).
   * @param debug_prefix Debug context prefix.
   */
  void add(poplar::Graph& graph, poplar::program::Sequence& prog,
           const std::vector<poplar::Tensor>& inputs,
           const std::vector<poplar::Tensor>& outputs,
           const poplar::DebugContext& debug_prefix) const {
    FMT_ASSERT(inputs.size() == inputs_info.size(),
               "Inconsistent inputs vector size.");
    FMT_ASSERT(outputs.size() == outputs_info.size(),
               "Inconsistent outputs vector size.");
    poplar::DebugContext debug_context(debug_prefix, this->pname);

    poplar::ComputeSet cs = graph.addComputeSet(debug_context);
    for (size_t tidx = 0; tidx < tiles.size(); ++tidx) {
      const auto tile = tiles[tidx];
      // Add vertex on the tile.
      auto v = graph.addVertex(cs, this->vname);
      graph.setTileMapping(v, tile);
      if (perf_estimate > 0) {
        graph.setPerfEstimate(v, perf_estimate);
      }
      // Map/connect vertex input tensors.
      for (size_t k = 0; k < inputs.size(); ++k) {
        const auto& info = inputs_info[k];
        graph.connect(v[info.name], info.connectReshape(inputs[k][tidx]));
      }
      // Map/connect vertex output tensors.
      for (size_t k = 0; k < outputs.size(); ++k) {
        // InOut tensors already mapped. Just need to connect pure output.
        if (outputs_info[k].iotype == VertexIOType::Out) {
          const auto& info = outputs_info[k];
          graph.connect(v[info.name], info.connectReshape(outputs[k][tidx]));
        }
      }
      // Map vertex attributes.
      for (const auto& attr : attributes_u32) {
        graph.setInitialValue(v[attr.name], attr.value);
      }
      for (const auto& attr : attributes_f32) {
        graph.setInitialValue(v[attr.name], attr.value);
      }
    }
    prog.add(poplar::program::Execute(cs, debug_context));
  }

  /**
   * @brief Add vertex/equation to Poplar graph & compute set (with outputs
   * allocated).
   *
   * @param graph Poplar graph.
   * @param prog Poplar sequence program.
   * @param inputs Vector of (sharded) input tensors.
   * @param debug_prefix Debug context prefix.
   * @return Vector of (tile sharded) output tensors.
   */
  std::vector<poplar::Tensor> add(
      poplar::Graph& graph, poplar::program::Sequence& prog,
      const std::vector<poplar::Tensor>& inputs,
      const poplar::DebugContext& debug_prefix) const {
    auto outputs = this->allocateOutputTensors(graph, inputs);
    this->add(graph, prog, inputs, outputs, debug_prefix);
    return outputs;
  }
};

NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE(TileMapEquation, pname, vname, tiles,
                                   inputs_info, outputs_info, attributes_u32,
                                   attributes_f32, gp_filename, perf_estimate)

}  // namespace ipu

/**
 * @brief IPU tile put sharded primitive: sharding an array over tiles on
 * the first axis.
 */
class TileMapEquationCall : public jax::ipu::PrimitiveInterface {
 public:
  static jax::ipu::PrimitiveMetadata metadata(std::uint32_t num_inputs) {
    // TODO. check InOut tensors for aliasing.
    return jax::ipu::PrimitiveMetadata{.num_inputs = num_inputs,
                                       .is_elementwise = false,
                                       .is_stateless = true,
                                       .is_hashable = true,
                                       .input_to_output_tensor_aliasing = {},
                                       .allocating_indices = {}};
  }

  static poplar::program::Program program(
      poplar::Graph& graph, const std::vector<poplar::Tensor>& inputs,
      std::vector<poplar::Tensor>& outputs, const std::string& attributes,
      const std::string& debug_prefix) {
    const auto debug_context = poplar::DebugContext(debug_prefix);
    // Deserialize tile mapped equation, and add to the graph.
    const auto tile_equation =
        ipu::from_json_str<ipu::TileMapEquation>(attributes);
    auto prog = poplar::program::Sequence();
    outputs = tile_equation.add(graph, prog, inputs, debug_context);
    return prog;
  }
};

// Export the IPU JAX primitives in the shared library.
EXPORT_IPU_JAX_PRIMITIVE(TileMapEquationCall);

// Declare a pybind11, to provide easy compilation & import from Python.
PYBIND11_MODULE(tile_interpreter_primitives_impl, m) {
  using namespace ipu;
  makeIpuTypeBindings(m);

  pybind11::enum_<VertexIOType>(m, "IpuVertexIOType", pybind11::arithmetic())
      .value("In", VertexIOType::In)
      .value("Out", VertexIOType::Out)
      .value("InOut", VertexIOType::InOut);
  makeVertexAttributeBindings<uint32_t>(m, "IpuVertexAttributeU32");
  makeVertexAttributeBindings<float>(m, "IpuVertexAttributeF32");

  pybind11::class_<ShapedArray>(m, "IpuShapedArray")
      .def(pybind11::init<>())
      .def(pybind11::init<const ShapeType&, IpuType>(), pybind11::arg("shape"),
           pybind11::arg("dtype"))
      .def("to_json_str", [](const ShapedArray& v) { return to_json_str(v); })
      .def_static(
          "from_json_str",
          [](const std::string& j) { return from_json_str<ShapedArray>(j); })
      .def_readwrite("shape", &ShapedArray::shape)
      .def_readwrite("dtype", &ShapedArray::dtype);

  pybind11::class_<VertexIOInfo>(m, "IpuVertexIOInfo")
      .def(pybind11::init<>())
      .def(pybind11::init<const std::string&, VertexIOType, const ShapedArray&,
                          uint8_t>(),
           pybind11::arg("name"), pybind11::arg("iotype"),
           pybind11::arg("aval"), pybind11::arg("rank") = 1)
      .def(pybind11::init<const std::string&, VertexIOType, const ShapeType&,
                          IpuType, uint8_t>(),
           pybind11::arg("name"), pybind11::arg("iotype"),
           pybind11::arg("shape"), pybind11::arg("dtype"),
           pybind11::arg("rank") = 1)
      .def(pybind11::self == pybind11::self)
      .def("to_json_str", [](const VertexIOInfo& v) { return to_json_str(v); })
      .def_static(
          "from_json_str",
          [](const std::string& j) { return from_json_str<VertexIOInfo>(j); })
      .def_readwrite("name", &VertexIOInfo::name)
      .def_readwrite("iotype", &VertexIOInfo::iotype)
      .def_readwrite("aval", &VertexIOInfo::aval)
      .def_readwrite("rank", &VertexIOInfo::rank)
      .def_property_readonly("shape",
                             [](const VertexIOInfo& v) { return v.aval.shape; })
      .def_property_readonly(
          "dtype", [](const VertexIOInfo& v) { return v.aval.dtype; });

  pybind11::class_<TileMapEquation>(m, "IpuTileMapEquation")
      .def(pybind11::init<>())
      .def(pybind11::init<const std::string&, const std::string&,
                          const std::vector<TileIndexType>&,
                          const std::vector<VertexIOInfo>&,
                          const std::vector<VertexIOInfo>&,
                          const std::vector<VertexAttributeU32>&,
                          const std::vector<VertexAttributeF32>&,
                          const std::string&, uint64_t>(),
           pybind11::arg("pname"), pybind11::arg("vname"),
           pybind11::arg("tiles"),
           pybind11::arg("inputs_info") = std::vector<VertexIOInfo>(),
           pybind11::arg("outputs_info") = std::vector<VertexIOInfo>(),
           pybind11::arg("attributes_u32") = std::vector<VertexAttributeU32>(),
           pybind11::arg("attributes_f32") = std::vector<VertexAttributeF32>(),
           pybind11::arg("gp_filename") = "",
           pybind11::arg("perf_estimate") = 0)
      .def("to_json_str",
           [](const TileMapEquation& v) { return to_json_str(v); })
      .def_static("from_json_str",
                  [](const std::string& j) {
                    return from_json_str<TileMapEquation>(j);
                  })
      .def_readwrite("pname", &TileMapEquation::pname)
      .def_readwrite("vname", &TileMapEquation::vname)
      .def_readwrite("tiles", &TileMapEquation::tiles)
      .def_readwrite("inputs_info", &TileMapEquation::inputs_info)
      .def_readwrite("outputs_info", &TileMapEquation::outputs_info)
      .def_readwrite("attributes_u32", &TileMapEquation::attributes_u32)
      .def_readwrite("attributes_f32", &TileMapEquation::attributes_f32)
      .def_readwrite("gp_filename", &TileMapEquation::gp_filename)
      .def_readwrite("perf_estimate", &TileMapEquation::perf_estimate);

  pybind11::class_<TileMapEquationCall>(m, "TileMapEquationCall")
      .def_static("metadata", &TileMapEquationCall::metadata,
                  pybind11::arg("num_inputs"));
}

// cppimport configuration for compiling the pybind11 module.
// clang-format off
/*
<%
cfg['extra_compile_args'] = ['-std=c++17', '-fPIC', '-O2', '-Wall']
cfg['libraries'] = ['poplar', 'poputil', 'popops']
cfg['include_dirs'] = []
setup_pybind11(cfg)
%>
*/
