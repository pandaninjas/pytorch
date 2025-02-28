import copy
import operator
from typing import Any, Callable, Tuple

import torch
from torch.fx import GraphModule, Node
from torch.fx.subgraph_rewriter import _replace_pattern
import torch.nn.functional as F
from torch.ao.quantization.fx._decomposed import quantized_decomposed_lib  # noqa: F401
from .utils import _fold_bn_weights_into_conv_node

# Example inputs for both `_conv2d_bn_pattern` and `_qat_conv2d_bn_pattern`
_conv2d_bn_pattern_example_inputs = (
    torch.randn(1, 1, 3, 3),  # x
    torch.randn(1, 1, 1, 1),  # conv_weight
    torch.randn(1),           # conv_bias
    torch.randn(1),           # bn_weight
    torch.randn(1),           # bn_bias
    torch.randn(1),           # bn_running_mean
    torch.randn(1),           # bn_running_var
)

# Example inputs for both `_quantized_qat_conv2d_bn_pattern` and `_folded_quantized_qat_conv2d_bn_pattern`
_quantized_conv2d_bn_pattern_example_inputs = (
    torch.randn(1, 1, 3, 3).to(torch.int8),  # x
    torch.randn(1, 1, 1, 1),  # conv_weight
    torch.randn(1),           # conv_bias
    torch.randn(1),           # bn_weight
    torch.randn(1),           # bn_bias
    torch.randn(1),           # bn_running_mean
    torch.randn(1),           # bn_running_var
    torch.tensor([1], dtype=torch.float),  # input_scale
    torch.tensor([0], dtype=torch.int),    # input_zero_point
    torch.tensor([1], dtype=torch.float),  # weight_scale
    torch.tensor([0], dtype=torch.int),    # weight_zero_point
    torch.tensor([1], dtype=torch.float),  # output_scale
    torch.tensor([0], dtype=torch.int),    # output_zero_point
)

def _conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    x = F.conv2d(x, conv_weight, conv_bias)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True)
    return x

def _qat_conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    """
    Approximated method to fuse conv and bn. It requires only one forward pass.
    conv_orig = conv / scale_factor where scale_factor = bn.weight / running_std.
    This is based on `nniqat.ConvBn2d._forward_approximate`.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    zero_bias = torch.zeros_like(conv_bias, dtype=x.dtype)
    x = F.conv2d(x, scaled_weight, zero_bias)
    x = x / scale_factor.reshape(bias_shape)
    x = x + conv_bias.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    return x

def _quantized_qat_conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
    input_scale: torch.Tensor,
    input_zero_point: torch.Tensor,
    weight_scale: torch.Tensor,
    weight_zero_point: torch.Tensor,
    output_scale: torch.Tensor,
    output_zero_point: torch.Tensor,
) -> torch.Tensor:
    """
    Quantized version of qat conv bn pattern,
    This is based on `nniqat.ConvBn2d._forward_approximate`.
    used in qat convert, we first match this pattern and then replace it with
    normal conv - bn pattern and then fold the weights of bn into conv
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    weight_quant_min = -127
    weight_quant_max = 127
    input_quant_min = -128
    input_quant_max = 127
    output_quant_min = -128
    output_quant_max = 127

    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    x = torch.ops.quantized_decomposed.dequantize_per_tensor(
        x, input_scale, input_zero_point, input_quant_min, input_quant_max, torch.int8)
    zero_bias = torch.zeros_like(conv_bias, dtype=x.dtype)
    scaled_weight = torch.ops.quantized_decomposed.quantize_per_tensor(
        scaled_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8)
    scaled_weight = torch.ops.quantized_decomposed.dequantize_per_tensor(
        scaled_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8)
    x = F.conv2d(x, scaled_weight, zero_bias)
    x = x / scale_factor.reshape(bias_shape)
    x = x + conv_bias.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    x = torch.ops.quantized_decomposed.quantize_per_tensor(
        x, output_scale, output_zero_point, output_quant_min, output_quant_max, torch.int8)
    return x

def _folded_quantized_qat_conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
    input_scale: torch.Tensor,
    input_zero_point: torch.Tensor,
    weight_scale: torch.Tensor,
    weight_zero_point: torch.Tensor,
    output_scale: torch.Tensor,
    output_zero_point: torch.Tensor,
) -> torch.Tensor:
    """ Quantized QAT conv - bn pattern with bn weights being folded into conv
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    weight_quant_min = -127
    weight_quant_max = 127
    input_quant_min = -128
    input_quant_max = 127
    output_quant_min = -128
    output_quant_max = 127

    x = torch.ops.quantized_decomposed.dequantize_per_tensor(
        x, input_scale, input_zero_point, input_quant_min, input_quant_max, torch.int8)
    conv_weight = torch.ops.quantized_decomposed.quantize_per_tensor(
        conv_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8)
    conv_weight = torch.ops.quantized_decomposed.dequantize_per_tensor(
        conv_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8)
    x = F.conv2d(x, conv_weight, conv_bias)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    x = torch.ops.quantized_decomposed.quantize_per_tensor(
        x, output_scale, output_zero_point, output_quant_min, output_quant_max, torch.int8)
    return x

def _get_aten_graph_module(
    pattern: Callable,
    example_inputs: Tuple[Any, ...],
) -> GraphModule:
    """
    Convert the pattern to an FX graph with decomposed aten ops.
    """
    # Avoid circular imports
    import torch._dynamo
    aten_pattern, _ = torch._dynamo.export(
        pattern,
        *copy.deepcopy(example_inputs),
        aten_graph=True,
        tracing_mode="real",
    )
    aten_pattern.graph.eliminate_dead_code()
    aten_pattern.recompile()
    return aten_pattern

def _fuse_conv_bn_qat(m: GraphModule) -> GraphModule:
    """
    Given a graph of decomposed aten ops, replace the (conv + bn) pattern with
    the fused QAT subgraph equivalent. The input graph should already be annotated.
    The annotations in the original nodes will be preserved in the corresponding
    nodes in the new subgraph.

    Note: This also handles the (conv + bn + relu) pattern.
    """
    m.graph.eliminate_dead_code()
    m.recompile()
    example_inputs = _conv2d_bn_pattern_example_inputs
    match_pattern = _get_aten_graph_module(_conv2d_bn_pattern, example_inputs)
    replacement_pattern = _get_aten_graph_module(_qat_conv2d_bn_pattern, example_inputs)
    # TODO: use the public replace_pattern API once it also returns replacement nodes
    match_and_replacement = _replace_pattern(m, match_pattern, replacement_pattern, ignore_literals=True)
    m.recompile()

    # Due to limited functionality in the subgraph rewriter, here we manually
    # update the replacement graph as follows:
    #
    #   (1) Copy over metadata from original subgraph. This ensures the stack traces
    #       and annotations are preserved in the new subgraph
    #
    #   (2) Copy over constant args for conv from the original subgraph
    #       TODO: do this for constant args for batchnorm as well
    #
    # In the future, we should try to push as much of this functionality into the
    # subgraph rewriter as possible, so we don't have to manually copy anything over.
    # For more detail, see https://github.com/pytorch/pytorch/issues/100419.

    for mr in match_and_replacement:
        replacement_conv_node = None
        replacement_bn_node = None
        replacement_getitem_node = None

        for replacement in mr.replacements:
            if (
                replacement.op == "call_function"
                and replacement.target == torch.ops.aten.convolution.default
            ):
                replacement_conv_node = replacement
            elif (
                replacement.op == "call_function"
                and replacement.target == torch.ops.aten._native_batch_norm_legit.default
            ):
                replacement_bn_node = replacement
            elif (
                replacement.op == "call_function"
                and replacement.target == operator.getitem
            ):
                replacement_getitem_node = replacement

        assert replacement_conv_node is not None
        assert replacement_bn_node is not None
        assert replacement_getitem_node is not None

        # Copy over metadata for all three nodes in [conv - bn - getitem]
        # Also copy over constant args for conv
        for original_node in mr.nodes_map.values():
            if original_node.target == torch.ops.aten.convolution.default:
                replacement_conv_node.meta = original_node.meta
                # Note: Unlike other tensor args like conv weights and biases, literal args are
                # preserved in the original nodes after replacement, so we can access them here
                # x, weight, bias, [stride, padding, dilation, transposed, output_padding, groups]
                replacement_conv_node.args = replacement_conv_node.args[:3] + original_node.args[3:]
            if original_node.target == torch.ops.aten._native_batch_norm_legit.default:
                replacement_bn_node.meta = original_node.meta
            if original_node.target == operator.getitem:
                replacement_getitem_node.meta = original_node.meta
    return m

def _fold_conv_bn_qat(m: GraphModule) -> GraphModule:
    """
    Replace the quantized (conv + bn) pattern with conv with bn weights folded into the weights of conv.
    """
    m.graph.eliminate_dead_code()
    m.recompile()
    example_inputs = _quantized_conv2d_bn_pattern_example_inputs
    match_pattern = _get_aten_graph_module(_quantized_qat_conv2d_bn_pattern, example_inputs)

    # Workaround: current convert does not produce q/dq ops with a specific overload
    # we'll remove the overload from the pattern here as a workaround since we do not want to break BC
    for n in match_pattern.graph.nodes:
        if n.op == "call_function" and n.target == torch.ops.quantized_decomposed.quantize_per_tensor.tensor:
            n.target = torch.ops.quantized_decomposed.quantize_per_tensor
        if n.op == "call_function" and n.target == torch.ops.quantized_decomposed.dequantize_per_tensor.tensor:
            n.target = torch.ops.quantized_decomposed.dequantize_per_tensor

    replacement_pattern = _get_aten_graph_module(_folded_quantized_qat_conv2d_bn_pattern, example_inputs)

    # TODO: use the public replace_pattern API once it also returns replacement nodes
    match_and_replacement = _replace_pattern(m, match_pattern, replacement_pattern, ignore_literals=True)
    m.recompile()

    for mr in match_and_replacement:
        # Find replacement conv and bn nodes by climbing upwards from anchor node
        assert len(mr.replacements) == 1, "expected only one replacement node"

        # find conv, bn, weight, bias nodes in the graph
        replacement_quantize_node = mr.replacements[0]
        assert replacement_quantize_node.target == torch.ops.quantized_decomposed.quantize_per_tensor.tensor
        n = replacement_quantize_node
        conv_node = None
        bn_node = None
        while conv_node is None or bn_node is None:
            if n.target == torch.ops.aten.convolution.default:
                conv_node = n
            if n.target == torch.ops.aten._native_batch_norm_legit.default:
                bn_node = n
            assert isinstance(n.args[0], Node)
            n = n.args[0]
        assert conv_node is not None and bn_node is not None

        conv_weight_dq = conv_node.args[1]
        assert conv_weight_dq.target == torch.ops.quantized_decomposed.dequantize_per_tensor.tensor
        conv_weight_q = conv_weight_dq.args[0]
        assert conv_weight_q.target == torch.ops.quantized_decomposed.quantize_per_tensor.tensor
        conv_weight = conv_weight_q.args[0]
        assert conv_weight.op == "get_attr"
        conv_bias = conv_node.args[2]

        # fold bn weights into conv
        _fold_bn_weights_into_conv_node(conv_node, conv_weight, conv_bias, bn_node, m)

    m.graph.eliminate_dead_code()
    m.recompile()
    return m
