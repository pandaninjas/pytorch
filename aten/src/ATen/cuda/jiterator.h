#pragma once

#include <c10/macros/Export.h>

#include <ATen/core/Tensor.h>

#include <string>
#include <vector>

namespace at {
namespace cuda {

TORCH_CUDA_CPP_API at::Tensor CompileKernel(
  const std::string& op_string,
  const std::string& optional_name,
  const std::string& optional_fusion_class,
  const std::vector<at::Tensor>& tensors);

}} // namespace at::cuda
