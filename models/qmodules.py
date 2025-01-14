import torch
from torch import nn
from torch.nn import functional as F
from torch.autograd import Function

from .common import autopad, Conv, C3, SPPF, Bottleneck
from .yolo import Detect

# quantized YOLOv5 modules
class Q_Conv(Conv):
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super().__init__(c1, c2, k=k, s=s, p=p, g=g, act=act)
        self.conv = Q_Conv2d(c1, c2, k, s, autopad(k, p), groups=g,
                             bias=False,
                             act_func='relu1' if c1 == 3 else 'swish')
        
        
        if c1 == 3 or c1 == 32:
            self.conv.a_quant._bitwidth.requires_grad = False
            self.conv.w_quant._bitwidth.requires_grad = False

class Q_C3(C3):
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5):  # ch_in, ch_out, number, shortcut, groups, expansion
        super().__init__(c1, c2, n=n, shortcut=shortcut, g=g, e=e)
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Q_Conv(c1, c_, 1, 1)
        self.cv2 = Q_Conv(c1, c_, 1, 1)
        self.cv3 = Q_Conv(2 * c_, c2, 1)  # optional act=FReLU(c2)
        self.m = nn.Sequential(*(Q_Bottleneck(c_, c_, shortcut, g, e=1.0) for _ in range(n)))

class Q_Bottleneck(Bottleneck):
    def __init__(self, c1, c2, shortcut=True, g=1, e=0.5):  # ch_in, ch_out, shortcut, groups, expansion
        super().__init__(c1, c2, shortcut=shortcut, g=g, e=e)
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Q_Conv(c1, c_, 1, 1)
        self.cv2 = Q_Conv(c_, c2, 3, 1, g=g)
        self.add = shortcut and c1 == c2

class Q_SPPF(SPPF):
    def __init__(self, c1, c2, k=5):  # equivalent to SPP(k=(5, 9, 13))
        super().__init__(c1, c2, k=k)
        c_ = c1 // 2  # hidden channels
        self.cv1 = Q_Conv(c1, c_, 1, 1)
        self.cv2 = Q_Conv(c_ * 4, c2, 1, 1)

class Q_Detect(Detect):
    def __init__(self, nc=80, anchors=(), ch=(), inplace=True):  # detection layer
        super().__init__(nc=nc, anchors=anchors, ch=ch, inplace=inplace)
        self.m = nn.ModuleList(Q_Conv2d(x, self.no * self.na, 1) for x in ch)  # output conv
# end of YOLOv5 quantized modules



class Q_Conv2d(nn.Conv2d):
    def __init__(self, *args, act_func=None, **kwargs):
        super().__init__(*args, **kwargs)
        # TODO activation & weight quantizer
        if(act_func is None):
            symm = True
        else:
            symm = False
        self.a_quant = Quantizer(act_func = act_func, symm = symm)
        self.w_quant = Quantizer(act_func = act_func, symm = symm)
    def forward(self, x):
        x = F.conv2d(self.a_quant(x), (self.w_quant(self.weight)), self.bias,
                self.stride, self.padding, self.dilation, self.groups)
        return x
    


class Q_Linear(nn.Linear):
    def __init__(self, *args, act_func=None, **kwargs):
        super().__init__(*args, **kwargs)
        # TODO activation & weight quantizer
        if(act_func is None):
            symm = True
        else:
            symm = False
        self.a_quant = Quantizer(act_func = act_func, symm=symm)
        self.w_quant = Quantizer(act_func = act_func, symm=symm)
    def forward(self, x):
        x = F.linear(self.a_quant(x), (self.w_quant(self.weight)), self.bias)
        return x


class noise_quant(Function):
    @staticmethod
    def forward(ctx, x, step):
        if x.is_cuda:
            rseed = torch.rand(1, device="cuda:0")[0]
            ctx.save_for_backward(rseed)
            torch.manual_seed(rseed)
            t1 = x + torch.round(torch.randn(1, device="cuda:0")[0]/2) * step
        else:
            rseed = torch.rand(1, device="cpu")[0]
            ctx.save_for_backward(rseed)
            torch.manual_seed(rseed)
            t1 = x + torch.round(torch.randn(1, device="cpu")[0]/2) * step
        return t1

    @staticmethod
    def backward(ctx, grad_output):
        rseed = ctx.saved_tensors[0]
        torch.manual_seed(rseed)
        grad_step = torch.round(torch.randn(1)[0]/2)

        return grad_output, grad_step


noise_quant = noise_quant.apply


def hard_quant(x):
    y_out = x.round()
    y_grad = x
    return (y_out-y_grad).detach() + y_grad


class Quantizer(nn.Module):
    def __init__(self, symm=False, act_func=None, *args, **kwargs):
        # symm : boolean, input distribution is symmetric or not
        # act_func : string, activation function used right before this
        #            quantizer. valid only if `symm` is `False`
        super(Quantizer, self).__init__(*args, **kwargs)
        self.symm = symm
        self.offset = None

        if not symm:
            act_func = act_func.lower()
            if 'relu' in act_func:  # ReLU, ReLU6
                self.offset = 0
            elif 'swish' in act_func:
                if 'h' in act_func[:2]:  # h-swish
                    self.offset = 0.3125
                else:  # swish
                    # from scipy.special import lambertw
                    # import math
                    # beta = 1.0  # NOTE input swish beta value
                    # self.offset = lambertw(1. / math.e) / beta
                    self.offset = 0.2784645427610738
            else:
                raise NotImplementedError
            self.offset = torch.tensor(self.offset, device='cuda')

        self._bitwidth = nn.Parameter(torch.tensor(2.5, device='cuda'))
        init_alpha = 0
        if symm:
            init_alpha = 1.0
        elif 'relu' in act_func or 'swish' in act_func:
            if act_func[-1].isdigit():
                init_alpha = float(act_func[-1])
            init_alpha = 6.0
        self._alpha = nn.Parameter(torch.tensor(init_alpha, device='cuda'))

    def bitwidth(self):
        def _logit2bit(logit):
            # assume round-to-nearest, round to even
            # 2.0 --> 2
            # 8.5 --> 8
            # 2.0 ~ 8.5
            # logit = 2.5 --> output = 8.006921768188477
            r = 6.5
            return 2.0 + torch.sigmoid(logit) * r

        if self._bitwidth.requires_grad:
            return _logit2bit(self._bitwidth)
        with torch.no_grad():
            return _logit2bit(self._bitwidth).round_()

    def alpha(self):
        return F.softplus(self._alpha)


    def _quant(self, x, is_training=True):
        # is_training : boolean
        #     if True,  apply diffQ
        #     if False, apply LSQ
        symm = self.symm
        offset = self.offset
        bitwidth = hard_quant(self.bitwidth())
        alpha = self.alpha()

        if symm:
            offset = alpha
            alpha = (2 - torch.pow(2., -bitwidth + 1)) * alpha
        if not x.is_cuda:
            bitwidth = bitwidth.cpu()
            alpha = alpha.cpu()
            offset = offset.cpu()

        n_lv = torch.pow(2., bitwidth) - 1
        n_lv_inv = torch.reciprocal(n_lv)

        step = alpha * n_lv_inv
        x = x + offset
        
        if is_training and self._bitwidth.requires_grad:
            x = torch.clamp((noise_quant(x, step))/alpha ,0 ,1) * alpha
            
        else:
            x = hard_quant(torch.clamp(x/alpha, 0, 1)*n_lv) * step

        return x - offset

    def forward(self, x):
        return self._quant(x, self.training)


def initialize_Q(model, mode='first', sample_input=None, channel_last=False):
    assert mode in ['first', 'finetune']
    if mode == 'first':
        assert sample_input is not None
        sample_activation_size(model, sample_input, channel_last)
    for m in model.modules():
        if isinstance(m, (Q_Conv2d, Q_Linear)):
            if mode == 'first':
                m.w_quant._alpha.data = m.weight.data.max()
            else:
                m.w_quant._bitwidth.requires_grad = False
                m.a_quant._bitwidth.requires_grad = False


class QuantOps(object):
    Conv2d = Q_Conv2d
    Linear = Q_Linear


def bops(k, c_in, c_out, h_out, w_out, w_bit, a_bit):
    # assume integer input for exact computation except `w_bit, a_bit`
    # return BitOPs (in terms of MAC(multiply-accumulate)) in Gi (GiBitOPs)
    # (1 GiBitOPs = 2 ** 30 BitOPs) # beware of subnormal values

    _bops = (c_out) * (c_in * k * k) * (w_out * h_out) * a_bit * w_bit
    _gibops = _bops / (2 ** 30)

    return _gibops


def model_bops(model):
    total_bops = torch.tensor([0.], device='cuda')
    conv = Q_Conv2d
    linear = Q_Linear
    for m in model.modules():
        if isinstance(m, conv):
            k = m.kernel_size[0]
            c_in = m.in_channels 
            c_out = m.out_channels
            h_out = m.out_height
            w_out = m.out_width
        elif isinstance(m, linear):
            k = 1
            c_in = m.in_features 
            c_out = 1
            h_out = 1
            w_out = m.out_features
            pass
        else:
            continue

        w_bit = m.w_quant.bitwidth()
        a_bit = m.a_quant.bitwidth()
        _bops = bops(k, c_in, c_out, h_out, w_out, w_bit, a_bit)
        total_bops = total_bops + _bops

    return total_bops


def bops_loss(model, target_bops, lambda_bops):
    total_bops = model_bops(model)
    return F.smooth_l1_loss(total_bops, target_bops) * lambda_bops


def sample_activation_size(model, x, channel_last=False):
    # forward hook for bops calculation (output h & w)
    # `out_height`, `out_width` for Conv2d
    hooks = []
    off_hooks = []

    def forward_hook(module, inputs, outputs):
        # pytorch default [batch, channel, height, width]
        offset = 2
        if channel_last:
            # tensorflow default [batch, height, width, channel]
            offset = 1
        module.in_height = inputs[0].shape[offset]
        module.in_width = inputs[0].shape[offset + 1]
        module.out_height = outputs.shape[offset]
        module.out_width = outputs.shape[offset + 1]

    def optimal_i(target, unit):
        min_err = 100
        min_err_i = -1
        for i in range(0, 5):
            err = abs(target - unit * i)
            if min_err > err:
                min_err = err
                min_err_i = i
        if min_err_i == -1:
            print('could not find optimal `i`')
            print(f'target={target}, unit={unit}')
            exit()
        return min_err_i

    def group_err(arr, offset, g=1):
        arr_len = len(arr)
        arr_err = torch.zeros_like(arr)
        arr_off = torch.zeros_like(arr)
        elem_per_group = arr_len // g
        for i in range(g):
            i0 = i * elem_per_group
            i1 = (i + 1) * elem_per_group
            if i == g - 1:
                off_unit = arr[i0:].min().item()
                off_unit = offset * optimal_i(off_unit, offset)
                arr_err[i0:] = abs(arr[i0:] - off_unit)
                arr_off[i0:] = arr[i0:] - arr[i0:] + off_unit
            else:
                off_unit = arr[i0:i1].min().item()
                off_unit = offset * optimal_i(off_unit, offset)
                arr_err[i0:i1] = abs(arr[i0:i1] - off_unit)
                arr_off[i0:i1] = arr[i0:i1] - arr[i0:i1] + off_unit

        return arr_err.mean().item(), arr_off

    def forward_off_hook(module, inputs, outputs):
        if hasattr(module, 'a_quant') and not module.a_quant.symm:
            c_idx = 3 if channel_last else 1
            permute_idx = [3, 0, 1, 2] if channel_last else [1, 0, 2, 3]
            num_channels = inputs[0].shape[c_idx]

            if num_channels == 3:
                return

            # get optimal number of groups `g`
            off = module.a_quant.offset
            min_err = 100
            min_err_g = 0
            c_min = inputs[0].permute(permute_idx).reshape(
                [num_channels, -1]).min(dim=1).values
            for g in range(1, 5):
                err, _ = group_err(c_min, -off, g)
                if min_err > err:
                    min_err = err
                    min_err_g = g

            # given number of groups `g`, get `module.offset`
            _, arr_off = group_err(c_min, -off, min_err_g)

            module.a_quant.offset = -arr_off.view([-1, 1, 1])
    for _, module in model.named_modules():
        if isinstance(module, nn.Conv2d):
            hooks.append(module.register_forward_hook(forward_hook))
        if isinstance(module, Q_Conv2d):
            off_hooks.append(module.register_forward_hook(forward_off_hook))

    with torch.no_grad():
        model.eval()
        model(x)

    for hook in hooks:
        hook.remove()
    for hook in off_hooks:
        hook.remove()

    return


def print_bitwidth(model, handle=None):
    str = []
    w_total_count = 0.
    avg_w_bits = 0.
    a_total_count = 0.
    avg_a_bits = 0.
    str.append('\t'.join(['layer', 'w_bits', 'a_bits']))
    for name, m in model.named_modules():
        if isinstance(m, (Q_Conv2d, Q_Linear)):
            str.append('\t'.join([
                f'{name}',
                f'{m.w_quant.bitwidth().item()}',
                f'{m.a_quant.bitwidth().item()}'
            ]))
            w_count = m.weight.view([-1]).shape[0] * (2. ** -30)
            w_total_count += w_count
            avg_w_bits += w_count * (m.w_quant.bitwidth() - avg_w_bits) / w_total_count

            a_count = 0.
            if isinstance(m, Q_Conv2d):
                a_count = m.in_height * m.in_width * m.in_channels * (2. ** -30)
            else:
                a_count = m.in_features * (2. ** -30)
            a_total_count += a_count
            avg_a_bits += a_count * (
                m.a_quant.bitwidth() - avg_a_bits) / a_total_count

    str.append(f'AVG w{avg_w_bits} a{avg_a_bits}')
    str.append(f'{model_bops(model).item()} GiBOPs')
    print('\n'.join(str))
    if handle is not None:
        with open(handle, 'a') as f:
            print('\n'.join(str), file=f)
    return
