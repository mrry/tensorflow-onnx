from tf2onnx.handler import tfl_op
from tf2onnx import constants, utils
import numpy as np

from tf2onnx.tf_loader import find_function
from tf2onnx.onnx_opset.controlflow import parameter_binding, inline_subgraph
from onnx.onnx_pb import TensorProto

def find_scan_outputs(while_node, while_g, body_g):
    print("hi")
    pass

@tfl_op(["TFL_WHILE"])
class TflWhile:
    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        tfl_while_inputs = node.input
        output_shapes = node.output_shapes
        output_dtypes = node.output_dtypes
        output_names = node.output

        cond_name = node.get_attr_str("cond_subgraph_index")
        cond_graph = find_function(cond_name)
        cond_graph.parent_graph = ctx

        body_name = node.get_attr_str("body_subgraph_index")
        body = find_function(body_name)
        body.parent_graph = ctx

        ctx.remove_node(node.name)

        cond_binding = parameter_binding(cond_graph, tfl_while_inputs)
        cond_outputs = inline_subgraph(ctx, cond_graph, cond_name, cond_binding)
        
        scan_outputs = sorted(body.scan_outputs, reverse=True)
        def input_is_unused(g, index):
            return len(g.find_output_consumers(g.inputs[index].output[0])) == 0
        scan_outputs = [(i, out) for i, out in scan_outputs if input_is_unused(cond_graph, i)]

        output_shapes = output_shapes
        output_dtypes = output_dtypes
        output_names = output_names

        for idx, _ in scan_outputs:
            del tfl_while_inputs[idx]
            output_shapes.append(output_shapes.pop(idx))
            output_dtypes.append(output_dtypes.pop(idx))
            output_names.append(output_names.pop(idx))

        max_iterations = ctx.make_const(utils.make_name("max_iterations"), np.array(np.iinfo(np.int64).max))

        loop_node = ctx.make_node("Loop", [max_iterations.output[0], cond_outputs[0]] + tfl_while_inputs,
                                  output_count=len(output_shapes), name=node.name + "_loop",
                                  shapes=output_shapes, dtypes=output_dtypes, skip_conversion=True)

        output_map = dict(zip(output_names, loop_node.output))

        # shift output consumers
        for k, v in output_map.items():
            ctx.replace_all_inputs(k, v)  # ops=ctx.get_nodes()

        wire_tfl_while_body(body, loop_node.inputs, output_shapes, output_dtypes, cond_graph, scan_outputs)
        find_scan_outputs(node, ctx, body)

        loop_node.set_body_graph_as_attr("body", body)

def wire_tfl_while_body(g, loop_node_inputs, output_shapes,
                        output_dtypes, cond_graph, scan_outputs):
    """Wire subgraph graph into main."""

    # onnx will pass in cond as argument
    iter_node = g.make_node("Placeholder", [], name=utils.make_name("iteration_num"),
                            output_count=1, dtypes=[TensorProto.INT64], shapes=[[]])
    cond_node = g.make_node("Placeholder", [], name=utils.make_name("cond"),
                            output_count=1, dtypes=[TensorProto.BOOL], shapes=[[]])
    cond_binding = parameter_binding(cond_graph, g.outputs)

    for idx, scan_output in scan_outputs:
        del g.func_inputs[idx]
        del g.outputs[idx]
        g.outputs.append(scan_output)

    # in onnx the body inputs are: index, cond, [loop_vars]
    g.func_inputs = [iter_node.output[0], cond_node.output[0]] + g.func_inputs
    # tell graph lib to keep inputs in order
    g._order_sensitive_inputs = \
        [g.get_node_by_output(name) for name in g.func_inputs]  # pylint: disable=protected-access

    for p, c in zip(loop_node_inputs, g.func_inputs):
        shape = p.output_shapes[0]
        g.set_shape(c, shape)
    
    cond_outputs = inline_subgraph(g, cond_graph, "cond__", cond_binding)

    g.outputs = [cond_outputs[0]] + g.outputs
    return g

@tfl_op(["TFL_IF"], tf_op="If")
class TflIfOp:
    @classmethod
    def to_tf(cls, ctx, node, **kwargs):
        then_branch = node.get_attr_str("then_subgraph_index")
        node.attr["then_branch"] = node.attr["then_subgraph_index"]
        del node.attr["then_subgraph_index"]
        node.attr["else_branch"] = node.attr["else_subgraph_index"]
        del node.attr["else_subgraph_index"]