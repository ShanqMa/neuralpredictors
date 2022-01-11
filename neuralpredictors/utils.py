import os, sys
import warnings
from contextlib import contextmanager
import numpy as np
import math
import h5py
import torch
from torch import nn as nn
from torch.nn import Parameter

from .training import eval_state


def flatten_json(nested_dict, keep_nested_name=True):
    """Turns a nested dictionary into a flattened dictionary. Designed to facilitate the populating of Config.Part tables
    with the corresponding config json list of parameters from the Config master table.

    Args:
        nested_dict: dict
            Nested dictionary to be flattened
        keep_nested_name: boolean, default True
            If True, record names will consist of all nested names separated by '_'. If False, last record name is
            chosen as new recod name. This is only possible for unique record names.

    Returns: dict
            Flattened dictionary

    Raises:
        ValueError: Multiple entries with identical names
    """
    out = {}

    def flatten(x, name=""):
        if isinstance(x, dict):
            for key, value in x.items():
                flatten(value, (name if keep_nested_name else "") + key + "_")
        else:
            if name[:-1] in out:
                raise ValueError("Multiple entries with identical names")
            out[name[:-1]] = x

    flatten(nested_dict)
    return out


def gini(x):
    """Calculates the Gini coefficient from a list of numbers. The Gini coefficient is used as a measure of (in)equality
    where a Gini coefficient of 1 (or 100%) expresses maximal inequality among values. A value greater than one may occur
     if some value represents negative contribution.

    Args:
        x: 1 D array or list
            Array of numbers from which to calculate the Gini coefficient.

    Returns: float
            Gini coefficient

    """
    x = np.asarray(x)  # The code below requires numpy arrays.
    if any(i < 0 for i in x):
        warnings.warn("Input x contains negative values")
    sorted_x = np.sort(x)
    n = len(x)
    cumx = np.cumsum(sorted_x, dtype=float)
    return (n + 1 - 2 * np.sum(cumx) / cumx[-1]) / n


def load_dict_from_hdf5(filename):
    """
    Given a `filename` of a HDF5 file, opens the file and
    load the entire content as a (nested) dictionary.

    Args:
        filename - name of HDF5 file

    Returns:
        (nested) dictionary corresponding to the content of the HDF5 file.
    """
    with h5py.File(filename, "r") as h5file:
        return recursively_load_dict_contents_from_group(h5file)


def recursively_load_dict_contents_from_group(h5file, path="/"):
    """
    Given a `h5file` h5py object, loads the object at `path`
    as nested dictionary.

    Args:
        h5file - h5py object
        path - Path within the h5py file to load the content of recursively.

    Returns:
        (nested) dictionary corresponding to the content of the HDF5 file at the path.
    """
    ans = {}
    for key, item in h5file[path].items():
        if isinstance(item, h5py.Dataset):
            dtype = item.dtype
            v = item[()]
            if dtype.char == "S":  # convert bytes to univcode
                v = v.astype(str)
            ans[key] = v
        elif isinstance(item, h5py.Group):
            if item.attrs.get("_iterable", False):
                ans[key] = [item[str(i)][()] for i in range(len(item))]
            else:
                ans[key] = recursively_load_dict_contents_from_group(h5file, path + key + "/")
    return ans


def get_module_output(model, input_shape, use_cuda=True):
    """
    Returns the output shape of the model when fed in an array of `input_shape`.
    Note that a zero array of shape `input_shape` is fed into the model and the
    shape of the output of the model is returned.

    Args:
        model (nn.Module): PyTorch module for which to compute the output shape
        input_shape (tuple): Shape specification for the input array into the model
        use_cuda (bool, optional): If True, model will be evaluated on CUDA if available. Othewrise
            model evaluation will take place on CPU. Defaults to True.

    Returns:
        tuple: output shape of the model

    """
    # infer the original device
    initial_device = next(iter(model.parameters())).device
    device = "cuda" if torch.cuda.is_available() and use_cuda else "cpu"
    with eval_state(model):
        with torch.no_grad():
            input = torch.zeros(1, *input_shape[1:], device=device)
            output = model.to(device)(input)
    model.to(initial_device)
    return output.shape


class BiasNet(nn.Module):
    """
    Small helper network that adds a learnable bias to an already instantiated base network
    """

    def __init__(self, base_net):
        super(BiasNet, self).__init__()
        self.bias = Parameter(torch.Tensor(2))
        self.base_net = base_net

    def forward(self, x):
        return self.base_net(x) + self.bias


class HiddenPrints:
    def __enter__(self):
        self._original_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._original_stdout


@contextmanager
def no_transforms(dat):
    transforms = dat.transforms
    try:
        dat.transforms = []
        yield dat
    finally:
        dat.transforms = transforms


class PositionalEncoding2D(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000, learned=False, width=None, height=None):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        if width is None:
            width = height = max_len

        if learned:
            self.twod_pe = nn.Parameter(torch.randn(d_model, (height * width)))
        else:
            d_model = d_model // 2
            pe = torch.zeros(width, d_model)
            position = torch.arange(0, width, dtype=torch.float).unsqueeze(1)
            div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
            pe[:, 0::2] = torch.sin(position * div_term)
            pe[:, 1::2] = torch.cos(position * div_term)
            pe = pe.unsqueeze(0)
            twod_pe = torch.zeros(height, width, d_model*2)
            for xpos in range(height):
                for ypos in range(width):
                    twod_pe[xpos, ypos, :] = torch.cat([pe[0, xpos], pe[0, ypos]], dim=-1)

            twod_pe = twod_pe.flatten(0,1).T
            self.register_buffer('twod_pe', twod_pe)

    def forward(self, x):
        x = x + self.twod_pe[:, :x.size(-1)].unsqueeze(0)
        return self.dropout(x)
