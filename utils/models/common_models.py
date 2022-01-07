# TODO: Refactor to a directory
import math
import torch
import torch.nn as nn
from torch.nn import functional as F

from ._utils import is_tracing, make_divisible
from .builder import MODELS


class non_bottleneck_1d(nn.Module):
    def __init__(self, chann, dropprob, dilated):
        super().__init__()
        self.conv3x1_1 = nn.Conv2d(chann, chann, (3, 1), stride=1, padding=(1, 0), bias=True)
        self.conv1x3_1 = nn.Conv2d(chann, chann, (1, 3), stride=1, padding=(0, 1), bias=True)
        self.bn1 = nn.BatchNorm2d(chann, eps=1e-03)
        self.conv3x1_2 = nn.Conv2d(chann, chann, (3, 1), stride=1, padding=(1 * dilated, 0),
                                   bias=True, dilation=(dilated, 1))
        self.conv1x3_2 = nn.Conv2d(chann, chann, (1, 3), stride=1, padding=(0, 1 * dilated),
                                   bias=True, dilation=(1, dilated))
        self.bn2 = nn.BatchNorm2d(chann, eps=1e-03)
        self.dropout = nn.Dropout2d(dropprob)

    def forward(self, input):
        output = self.conv3x1_1(input)
        output = F.relu(output)
        output = self.conv1x3_1(output)
        output = self.bn1(output)
        output = F.relu(output)

        output = self.conv3x1_2(output)
        output = F.relu(output)
        output = self.conv1x3_2(output)
        output = self.bn2(output)

        if self.dropout.p != 0:
            output = self.dropout(output)

        return F.relu(output + input)


# Unused
# SCNN original decoder for ResNet-101, very large channels, maybe impossible to add anything
# resnet-101 -> H x W x 2048
# 3x3 Conv -> H x W x 512
# Dropout 0.1
# 1x1 Conv -> H x W x 5
# https://github.com/XingangPan/SCNN/issues/35
@MODELS.register()
class SCNNDecoder(nn.Module):
    def __init__(self, in_channels=2048, num_classes=5):
        super(SCNNDecoder, self).__init__()
        out_channels = in_channels // 4
        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.dropout1 = nn.Dropout2d(0.1)
        self.conv2 = nn.Conv2d(out_channels, num_classes, 1, bias=False)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.dropout1(x)
        x = self.conv2(x)

        return x


# Plain decoder (albeit simplest) from the RESA paper
@MODELS.register()
class PlainDecoder(nn.Module):
    def __init__(self, in_channels=128, num_classes=5):
        super(PlainDecoder, self).__init__()
        self.dropout1 = nn.Dropout2d(0.1)
        self.conv1 = nn.Conv2d(in_channels, num_classes, 1, bias=True)

    def forward(self, x):
        x = self.dropout1(x)
        x = self.conv1(x)

        return x


# Added a coarse path to the original ERFNet UpsamplerBlock
# Copied and modified from:
# https://github.com/ZJULearning/resa/blob/14b0fea6a1ab4f45d8f9f22fb110c1b3e53cf12e/models/decoder.py#L67
class BilateralUpsamplerBlock(nn.Module):
    def __init__(self, ninput, noutput):
        super(BilateralUpsamplerBlock, self).__init__()
        self.conv = nn.ConvTranspose2d(ninput, noutput, 3, stride=2, padding=1, output_padding=1, bias=True)
        self.bn = nn.BatchNorm2d(noutput, eps=1e-3, track_running_stats=True)
        self.follows = nn.ModuleList(non_bottleneck_1d(noutput, 0, 1) for _ in range(2))

        # interpolate
        self.interpolate_conv = nn.Conv2d(ninput, noutput, kernel_size=1, bias=False)
        self.interpolate_bn = nn.BatchNorm2d(noutput, eps=1e-3)

    def forward(self, input):
        # Fine branch
        output = self.conv(input)
        output = self.bn(output)
        out = F.relu(output)
        for follow in self.follows:
            out = follow(out)

        # Coarse branch (keep at align_corners=True)
        interpolate_output = self.interpolate_conv(input)
        interpolate_output = self.interpolate_bn(interpolate_output)
        interpolate_output = F.relu(interpolate_output)
        interpolated = F.interpolate(interpolate_output, size=out.shape[-2:], mode='bilinear', align_corners=True)

        return out + interpolated


# Bilateral Up-Sampling Decoder in RESA paper,
# make it work for arbitrary input channels (8x up-sample then predict).
# Drops transposed prediction layer in ERFNet, while adds an extra up-sampling block.
@MODELS.register()
class BUSD(nn.Module):
    def __init__(self, in_channels=128, num_classes=5):
        super(BUSD, self).__init__()
        base = in_channels // 8
        self.layers = nn.ModuleList(BilateralUpsamplerBlock(ninput=base * 2 ** (3 - i), noutput=base * 2 ** (2 - i))
                                    for i in range(3))
        self.output_proj = nn.Conv2d(base, num_classes, kernel_size=1, bias=True)  # Keep bias=True for prediction

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)

        return self.output_proj(x)


# Reduce channel (typically to 128), RESA code use no BN nor ReLU
@MODELS.register()
class RESAReducer(nn.Module):
    def __init__(self, in_channels=512, reduce=128, bn_relu=True):
        super(RESAReducer, self).__init__()
        self.bn_relu = bn_relu
        self.conv1 = nn.Conv2d(in_channels, reduce, 1, bias=False)
        if self.bn_relu:
            self.bn1 = nn.BatchNorm2d(reduce)

    def forward(self, x):
        x = self.conv1(x)
        if self.bn_relu:
            x = self.bn1(x)
            x = F.relu(x)

        return x


# SCNN
@MODELS.register()
class SpatialConv(nn.Module):
    def __init__(self, num_channels=128):
        super().__init__()
        self.conv_d = nn.Conv2d(num_channels, num_channels, (1, 9), padding=(0, 4))
        self.conv_u = nn.Conv2d(num_channels, num_channels, (1, 9), padding=(0, 4))
        self.conv_r = nn.Conv2d(num_channels, num_channels, (9, 1), padding=(4, 0))
        self.conv_l = nn.Conv2d(num_channels, num_channels, (9, 1), padding=(4, 0))
        self._adjust_initializations(num_channels=num_channels)

    def _adjust_initializations(self, num_channels=128):
        # https://github.com/XingangPan/SCNN/issues/82
        bound = math.sqrt(2.0 / (num_channels * 9 * 5))
        nn.init.uniform_(self.conv_d.weight, -bound, bound)
        nn.init.uniform_(self.conv_u.weight, -bound, bound)
        nn.init.uniform_(self.conv_r.weight, -bound, bound)
        nn.init.uniform_(self.conv_l.weight, -bound, bound)

    def forward(self, input):
        output = input

        if is_tracing():
            # PyTorch index+add_ will be ignored in traced graph
            # Down
            for i in range(1, output.shape[2]):
                output[:, :, i:i + 1, :] = output[:, :, i:i + 1, :].add(F.relu(self.conv_d(output[:, :, i - 1:i, :])))
            # Up
            for i in range(output.shape[2] - 2, 0, -1):
                output[:, :, i:i + 1, :] = output[:, :, i:i + 1, :].add(
                    F.relu(self.conv_u(output[:, :, i + 1:i + 2, :])))
            # Right
            for i in range(1, output.shape[3]):
                output[:, :, :, i:i + 1] = output[:, :, :, i:i + 1].add(F.relu(self.conv_r(output[:, :, :, i - 1:i])))
            # Left
            for i in range(output.shape[3] - 2, 0, -1):
                output[:, :, :, i:i + 1] = output[:, :, :, i:i + 1].add(
                    F.relu(self.conv_l(output[:, :, :, i + 1:i + 2])))
        else:
            # First one remains unchanged (according to the original paper), why not add a relu afterwards?
            # Update and send to next
            # Down
            for i in range(1, output.shape[2]):
                output[:, :, i:i + 1, :].add_(F.relu(self.conv_d(output[:, :, i - 1:i, :])))
            # Up
            for i in range(output.shape[2] - 2, 0, -1):
                output[:, :, i:i + 1, :].add_(F.relu(self.conv_u(output[:, :, i + 1:i + 2, :])))
            # Right
            for i in range(1, output.shape[3]):
                output[:, :, :, i:i + 1].add_(F.relu(self.conv_r(output[:, :, :, i - 1:i])))
            # Left
            for i in range(output.shape[3] - 2, 0, -1):
                output[:, :, :, i:i + 1].add_(F.relu(self.conv_l(output[:, :, :, i + 1:i + 2])))

        return output


# REcurrent Feature-Shift Aggregator in RESA paper
@MODELS.register()
class RESA(nn.Module):
    def __init__(self, num_channels=128, iteration=5, alpha=2.0, trace_arg=None):
        super(RESA, self).__init__()
        # Different from SCNN, RESA uses bias=False & different convolution layers for each stride,
        # i.e. 4 * iteration layers vs. 4 layers in SCNN, maybe special init is not needed anymore:
        # https://github.com/ZJULearning/resa/blob/14b0fea6a1ab4f45d8f9f22fb110c1b3e53cf12e/models/resa.py#L21
        self.iteration = iteration
        self.alpha = alpha
        self.conv_d = nn.ModuleList(nn.Conv2d(num_channels, num_channels, (1, 9), padding=(0, 4), bias=False)
                                    for _ in range(iteration))
        self.conv_u = nn.ModuleList(nn.Conv2d(num_channels, num_channels, (1, 9), padding=(0, 4), bias=False)
                                    for _ in range(iteration))
        self.conv_r = nn.ModuleList(nn.Conv2d(num_channels, num_channels, (9, 1), padding=(4, 0), bias=False)
                                    for _ in range(iteration))
        self.conv_l = nn.ModuleList(nn.Conv2d(num_channels, num_channels, (9, 1), padding=(4, 0), bias=False)
                                    for _ in range(iteration))
        self._adjust_initializations(num_channels=num_channels)
        if trace_arg is not None:  # Pre-compute offsets for a TensorRT supported implementation
            h = (trace_arg['h'] - 1) // 8 + 1
            w = (trace_arg['w'] - 1) // 8 + 1
            self.offset_h = []
            self.offset_w = []
            for i in range(self.iteration):
                self.offset_h.append(h // 2 ** (self.iteration - i))
                self.offset_w.append(w // 2 ** (self.iteration - i))

    def _adjust_initializations(self, num_channels=128):
        # https://github.com/XingangPan/SCNN/issues/82
        bound = math.sqrt(2.0 / (num_channels * 9 * 5))
        for i in self.conv_d:
            nn.init.uniform_(i.weight, -bound, bound)
        for i in self.conv_u:
            nn.init.uniform_(i.weight, -bound, bound)
        for i in self.conv_r:
            nn.init.uniform_(i.weight, -bound, bound)
        for i in self.conv_l:
            nn.init.uniform_(i.weight, -bound, bound)

    def forward(self, x):
        y = x
        h, w = y.shape[-2:]
        if 2 ** self.iteration > max(h, w):
            print('Too many iterations for RESA, your image size may be too small.')

        # We do indexing here to avoid extra input parameters at __init__(), with almost none computation overhead.
        # Also, now it won't block arbitrary shaped input.
        # However, we still need an alternative to Gather for TensorRT
        # Down
        for i in range(self.iteration):
            if is_tracing():
                temp = torch.cat([y[:, :, self.offset_h[i]:, :], y[:, :, :self.offset_h[i], :]], dim=-2)
                y = y.add(self.alpha * F.relu(self.conv_d[i](temp)))
            else:
                idx = (torch.arange(h) + h // 2 ** (self.iteration - i)) % h
                y.add_(self.alpha * F.relu(self.conv_d[i](y[:, :, idx, :])))
        # Up
        for i in range(self.iteration):
            if is_tracing():
                temp = torch.cat([y[:, :, (h - self.offset_h[i]):, :], y[:, :, :(h - self.offset_h[i]), :]], dim=-2)
                y = y.add(self.alpha * F.relu(self.conv_u[i](temp)))
            else:
                idx = (torch.arange(h) - h // 2 ** (self.iteration - i)) % h
                y.add_(self.alpha * F.relu(self.conv_u[i](y[:, :, idx, :])))
        # Right
        for i in range(self.iteration):
            if is_tracing():
                temp = torch.cat([y[:, :, :, self.offset_w[i]:], y[:, :, :, :self.offset_w[i]]], dim=-1)
                y = y.add(self.alpha * F.relu(self.conv_r[i](temp)))
            else:
                idx = (torch.arange(w) + w // 2 ** (self.iteration - i)) % w
                y.add_(self.alpha * F.relu(self.conv_r[i](y[:, :, :, idx])))
        # Left
        for i in range(self.iteration):
            if is_tracing():
                temp = torch.cat([y[:, :, :, (w - self.offset_w[i]):], y[:, :, :, :(w - self.offset_w[i])]], dim=-1)
                y = y.add(self.alpha * F.relu(self.conv_l[i](temp)))
            else:
                idx = (torch.arange(w) - w // 2 ** (self.iteration - i)) % w
                y.add_(self.alpha * F.relu(self.conv_l[i](y[:, :, :, idx])))

        return y


# Typical lane existence head originated from the SCNN paper
@MODELS.register()
class SimpleLaneExist(nn.Module):
    def __init__(self, num_output, flattened_size=4500):
        super().__init__()
        self.avgpool = nn.AvgPool2d(2, 2)
        self.linear1 = nn.Linear(flattened_size, 128)
        self.linear2 = nn.Linear(128, num_output)

    def forward(self, input, predict=False):
        # input: logits
        output = self.avgpool(input)
        output = output.flatten(start_dim=1)
        output = self.linear1(output)
        output = F.relu(output)
        output = self.linear2(output)
        if predict:
            output = torch.sigmoid(output)

        return output


# Lane exist head for ERFNet, ENet
# Really tricky without global pooling
@MODELS.register()
class EDLaneExist(nn.Module):
    def __init__(self, num_output, flattened_size=3965, dropout=0.1, pool='avg'):
        super().__init__()

        self.layers = nn.ModuleList()
        self.layers.append(nn.Conv2d(128, 32, (3, 3), stride=1, padding=(4, 4), bias=False, dilation=(4, 4)))
        self.layers.append(nn.BatchNorm2d(32, eps=1e-03))

        self.layers_final = nn.ModuleList()
        self.layers_final.append(nn.Dropout2d(dropout))
        self.layers_final.append(nn.Conv2d(32, 5, (1, 1), stride=1, padding=(0, 0), bias=True))

        if pool == 'max':
            self.pool = nn.MaxPool2d(2, stride=2)
        elif pool == 'avg':
            self.pool = nn.AvgPool2d(2, stride=2)
        else:
            raise RuntimeError("This type of pool has not been defined yet!")

        self.linear1 = nn.Linear(flattened_size, 128)
        self.linear2 = nn.Linear(128, num_output)

    def forward(self, input):
        output = input
        for layer in self.layers:
            output = layer(output)

        output = F.relu(output)

        for layer in self.layers_final:
            output = layer(output)

        output = F.softmax(output, dim=1)
        output = self.pool(output)
        output = output.flatten(start_dim=1)
        output = self.linear1(output)
        output = F.relu(output)
        output = self.linear2(output)

        return output


@MODELS.register()
class RESALaneExist(nn.Module):
    def __init__(self, num_output, flattened_size=3965, dropout=0.1, in_channels=128):
        super().__init__()

        self.layers = nn.ModuleList()
        self.layers.append(nn.Dropout2d(dropout))
        self.layers.append(nn.Conv2d(in_channels, num_output + 1, (1, 1), stride=1, padding=(0, 0), bias=True))
        self.pool = nn.AvgPool2d(2, stride=2)
        self.linear1 = nn.Linear(flattened_size, 128)
        self.linear2 = nn.Linear(128, num_output)

    def forward(self, input):
        output = input
        for layer in self.layers:
            output = layer(output)
        output = F.softmax(output, dim=1)
        output = self.pool(output)
        output = output.flatten(start_dim=1)
        output = self.linear1(output)
        output = F.relu(output)
        output = self.linear2(output)

        return output


# MobileV2
class InvertedResidual(nn.Module):
    """InvertedResidual block for MobileNetV2.
    Args:
        in_channels (int): The input channels of the InvertedResidual block.
        out_channels (int): The output channels of the InvertedResidual block.
        stride (int): Stride of the middle (first) 3x3 convolution.
        expand_ratio (int): Adjusts number of channels of the hidden layer
            in InvertedResidual by this amount.
        dilation (int): Dilation rate of depthwise conv. Default: 1
    Returns:
        Tensor: The output tensor.
    """

    def __init__(self, in_channels, out_channels, stride, expand_ratio, dilation=1, bias=False):
        super(InvertedResidual, self).__init__()
        self.stride = stride
        assert stride in [1, 2], f'stride must in [1, 2]. ' \
                                 f'But received {stride}.'
        self.use_res_connect = self.stride == 1 and in_channels == out_channels
        hidden_dim = int(round(in_channels * expand_ratio))
        layers = []
        if expand_ratio != 1:
            layers.extend([
                nn.Conv2d(in_channels=in_channels, out_channels=hidden_dim, kernel_size=1, bias=bias),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6()  # min(max(0, x), 6)
            ])

        layers.extend([
            nn.Conv2d(in_channels=hidden_dim, out_channels=hidden_dim, kernel_size=3, stride=stride,
                      padding=dilation, dilation=dilation, groups=hidden_dim, bias=bias),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU6(),
            nn.Conv2d(in_channels=hidden_dim, out_channels=out_channels, kernel_size=1, bias=bias),
            nn.BatchNorm2d(out_channels)
        ])
        self.conv = nn.Sequential(*layers)

    def forward(self, x):

        def _inner_forward(x):
            if self.use_res_connect:
                return x + self.conv(x)
            else:
                return self.conv(x)

        out = _inner_forward(x)

        return out


class InvertedResidualV3(nn.Module):
    """Inverted Residual Block for MobileNetV3.
    Args:
        in_channels (int): The input channels of this Module.
        out_channels (int): The output channels of this Module.
        mid_channels (int): The input channels of the depthwise convolution.
        kernel_size (int): The kernel size of the depthwise convolution. Default: 3.
        stride (int): The stride of the depthwise convolution. Default: 1.
        with_se (dict): with or without se layer. Default: False, which means no se layer.
        with_expand_conv (bool): Use expand conv or not. If set False,
            mid_channels must be the same with in_channels. Default: True.
        act_cfg (dict): Config dict for activation layer.
            Default: dict(type='ReLU').
    Returns:
        Tensor: The output tensor.
    """

    def __init__(self, in_channels, out_channels, mid_channels, kernel_size=3, stride=1, with_se=False,
                 with_expand_conv=True, act='HSwish', bias=False, dilation=1):
        super(InvertedResidualV3, self).__init__()
        self.with_res_shortcut = (stride == 1 and in_channels == out_channels)
        assert stride in [1, 2]
        activation_layer = nn.Hardswish if act == 'HSwish' else nn.ReLU6
        self.with_se = with_se
        self.with_expand_conv = with_expand_conv
        if not self.with_expand_conv:
            assert mid_channels == in_channels
        if self.with_expand_conv:
            self.expand_conv = nn.Sequential(
                nn.Conv2d(in_channels=in_channels, out_channels=mid_channels, kernel_size=1, stride=1, padding=0,
                          bias=bias),
                nn.BatchNorm2d(mid_channels),
                activation_layer()
            )
        if stride > 1 and dilation > 1:
            raise ValueError('Can\'t have stride and dilation both > 1 in MobileNetV3')
        self.depthwise_conv = nn.Sequential(
            nn.Conv2d(in_channels=mid_channels, out_channels=mid_channels, kernel_size=kernel_size, stride=stride,
                      padding=(kernel_size - 1) // 2 * dilation, dilation=dilation, groups=mid_channels, bias=bias),
            nn.BatchNorm2d(mid_channels),
            activation_layer()
        )
        if self.with_se:
            self.se = SELayer(channels=mid_channels, ratio=4)

        self.linear_conv = nn.Sequential(
            nn.Conv2d(in_channels=mid_channels, out_channels=out_channels, kernel_size=1, stride=1, padding=0,
                      bias=False),
            nn.BatchNorm2d(out_channels)
        )

    def forward(self, x):

        def _inner_forward(x):
            out = x

            if self.with_expand_conv:
                out = self.expand_conv(out)

            out = self.depthwise_conv(out)

            if self.with_se:
                out = self.se(out)

            out = self.linear_conv(out)

            if self.with_res_shortcut:
                return x + out
            else:
                return out

        out = _inner_forward(x)

        return out


class SELayer(nn.Module):
    """Squeeze-and-Excitation Module.
    Args:
        channels (int): The input (and output) channels of the SE layer.
        ratio (int): Squeeze ratio in SELayer, the intermediate channel will be
            ``int(channels/ratio)``. Default: 16.
    """

    def __init__(self, channels, ratio=16, act=nn.ReLU, scale_act=nn.Sigmoid):
        super(SELayer, self).__init__()

        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Conv2d(in_channels=channels, out_channels=make_divisible(channels // ratio, 8), kernel_size=1,
                             stride=1)
        self.fc2 = nn.Conv2d(in_channels=make_divisible(channels // ratio, 8), out_channels=channels, kernel_size=1,
                             stride=1)
        self.activation = act()
        self.scale_activation = scale_act()

    def forward(self, x):
        out = self.avgpool(x)
        out = self.fc1(out)
        out = self.activation(out)
        out = self.fc2(out)
        out = self.scale_activation(out)
        return x * out


class PPM(nn.ModuleList):
    """
    Pooling pyramid module used in PSPNet
    Args:
        pool_scales(tuple(int)): Pooling scales used in pooling Pyramid Module
        applied on the last feature. default: (1, 2, 3, 6)
    """

    def __init__(self, pool_scales, in_channels, channels, align_corners=False):
        super(PPM, self).__init__()
        self.pool_scales = pool_scales
        self.align_corners = align_corners
        self.in_channels = in_channels
        self.channels = channels
        for pool_scale in pool_scales:
            self.append(
                nn.Sequential(
                    nn.AdaptiveAvgPool2d(pool_scale),
                    nn.Conv2d(self.in_channels, self.channels, 1),
                    nn.BatchNorm2d(self.channels),
                    nn.ReLU()))

    def forward(self, x):
        ppm_outs = []
        for ppm in self:
            ppm_out = ppm(x)
            upsampled_ppm_out = F.interpolate(ppm_out, size=x.size()[2:], mode='bilinear',
                                              align_corners=self.align_corners)
            ppm_outs.append(upsampled_ppm_out)
        return ppm_outs

@MODELS.register()
class UperHead(nn.Module):
    def __init__(self, in_channels, channels, pool_scales=(1, 2, 3, 6), align_corners=False):
        super(UperHead, self).__init__()
        self.in_channels = in_channels
        self.channels = channels
        self.align_corners = align_corners
        # PSP module
        self.psp_modules = PPM(pool_scales=pool_scales, in_channels=self.in_channels[-1], channels=self.channels,
                               align_corners=align_corners)
        self.psp_bottleneck = nn.Sequential(
            nn.Conv2d(in_channels=self.in_channels[-1] + len(pool_scales) * self.channels, out_channels=self.channels,
                      kernel_size=3, padding=1),
            nn.BatchNorm2d(self.channels),
            nn.ReLU()
        )
        # FPN module
        self.lateral_convs = nn.ModuleList()
        self.fpn_convs = nn.ModuleList()
        for in_channel in self.in_channels[:-1]:
            lateral_conv = nn.Sequential(
                nn.Conv2d(in_channel, self.channels, 1),
                nn.BatchNorm2d(self.channels),
                nn.ReLU())
            fpn_conv = nn.Sequential(
                nn.Conv2d(self.channels, self.channels, 3, padding=1),
                nn.BatchNorm2d(self.channels),
                nn.ReLU()
            )
            self.lateral_convs.append(lateral_conv)
            self.fpn_convs.append(fpn_conv)
        self.fpn_bottleneck = nn.Sequential(
            nn.Conv2d(len(self.in_channels) * self.channels, self.channels, 3, padding=1),
            nn.BatchNorm2d(self.channels),
            nn.ReLU()
        )

    def psp_forward(self, inputs):
        # forward function for psp module
        x = inputs[-1]
        psp_outs = [x]
        psp_outs.extend(self.psp_modules(x))
        psp_outs = torch.cat(psp_outs, dim=1)
        outputs = self.psp_bottleneck(psp_outs)

        return outputs

    def forward(self, inputs):
        assert isinstance(inputs, tuple), 'inputs must be a tuple'
        inputs = list(inputs)
        # build laterals
        laterals = [lateral_conv(inputs[i]) for i, lateral_conv in enumerate(self.lateral_convs)]
        laterals.append(self.psp_forward(inputs))
        # build top-down path
        used_backbone_levels = len(laterals)
        for i in range(used_backbone_levels - 1, 0, -1):
            prev_shape = laterals[i - 1].shape[2:]
            laterals[i - 1] = laterals[i - 1] + F.interpolate(laterals[i], size=prev_shape, mode='bilinear',
                                                              align_corners=self.align_corners)
        # build outputs
        fpn_outs = [self.fpn_convs[i](laterals[i]) for i in range(used_backbone_levels - 1)]
        # add psp feature
        fpn_outs.append(laterals[-1])
        for i in range(used_backbone_levels - 1, 0, -1):
            fpn_outs[i] = F.interpolate(fpn_outs[i], fpn_outs[0].shape[2:], mode='bilinear',
                                       align_corners=self.align_corners)
        fpn_outs = torch.cat(fpn_outs, dim=1)
        output = self.fpn_bottleneck(fpn_outs)
        return output