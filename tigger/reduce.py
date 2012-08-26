import numpy
from tigger.cluda import dtypes
from tigger.core import *
from tigger.transpose import Transpose

TEMPLATE = template_for(__file__)


def reduced_shape(shape, axis):
    l = list(shape)
    l.pop(axis)
    return tuple(l)


class Reduce(Computation):

    def _get_default_basis(self):
        return dict(shape=(1,1), dtype=numpy.float32, axis=-1,
            operation="return val1 + val2;")

    def _construct_basis(self, output, input, axis=None, operation=None):
        assert input.dtype == output.dtype
        assert input.size % output.size == 0
        assert input.size > output.size

        bs = dict(shape=input.shape, dtype=input.dtype)

        if axis is not None:
            bs['axis'] = axis if axis >= 0 else len(input.shape) + axis
        if operation is not None:
            bs['operation'] = operation

        return bs

    def _get_base_signature(self, basis):
        return ([('output', ArrayValue(reduced_shape(basis.shape, basis.axis), basis.dtype))],
            [('input', ArrayValue(basis.shape, basis.dtype))],
            [])

    def _construct_operations(self, basis, device_params, operations):

        # may fail if the user passes particularly sophisticated operation
        max_reduce_power = device_params.max_work_group_size

        axis = basis.axis

        size = product(basis.shape)
        final_size = product(reduced_shape(basis.shape, basis.axis))

        if len(basis.shape) == 1 or axis == len(basis.shape) - 1:
            # normal reduction
            input_name = 'input'
        else:
            tr_axes = tuple(xrange(len(basis.shape)))
            tr_axes = tr_axes[:axis] + tr_axes[axis+1:] + (axis,)
            tr_shape = tuple(basis.shape[i] for i in tr_axes)

            operations.add_allocation('_tr_output', tr_shape, basis.dtype)

            transpose = self.get_nested_computation(Transpose)
            transpose.set_basis_for(operations.values['_tr_output'], operations.values['input'],
                axes=tr_axes)
            operations.add_computation(transpose, '_tr_output', 'input')
            input_name = '_tr_output'

        reduction_stage = 0
        while size > final_size:
            reduction_stage += 1

            part_size = size / final_size

            if part_size >= max_reduce_power:
                block_size = max_reduce_power
                blocks_per_part = min_blocks(part_size, block_size)
                blocks_num = blocks_per_part * final_size
                last_block_size = part_size - (blocks_per_part - 1) * block_size
                new_size = blocks_num
            else:
                block_size = 2 ** (log2(size / final_size - 1) + 1)
                blocks_per_part = 1
                blocks_num = final_size
                last_block_size = size / final_size
                new_size = final_size

            global_size = blocks_num * block_size

            if new_size != final_size:
                temp_name = '_temp' + str(reduction_stage)
                operations.add_allocation(temp_name, (new_size,), basis.dtype)
                output_name = temp_name
            else:
                output_name = 'output'

            render_kwds = dict(
                blocks_per_part=blocks_per_part, last_block_size=last_block_size,
                log2=log2, block_size=block_size,
                warp_size=device_params.warp_size,
                operation_code=basis.operation)

            operations.add_kernel(
                TEMPLATE, 'reduce', [output_name, input_name],
                global_size=(global_size,), local_size=(block_size,), render_kwds=render_kwds)

            size = new_size
            input_name = output_name
