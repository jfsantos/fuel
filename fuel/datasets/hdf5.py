from collections import defaultdict

import h5py
import tables

from fuel.datasets import Dataset
from fuel.utils import do_not_pickle_attributes


@do_not_pickle_attributes('nodes')
class Hdf5Dataset(Dataset):
    """An HDF5 dataset.

    Parameters
    ----------
    sources : tuple of strings
        Sources which the dataset returns
    start : int
        Start index
    stop : int
        Stop index
    data_node : str
        Parent data node in HDF5 file
    sources_in_file : tuple of strings
        Names of nodes in HDF5 file which contain sources. Should the same
        length as `sources`.
        Optional, if not set will be equal to `sources`.

    """
    def __init__(self, sources, start, stop, path, data_node='Data',
                 sources_in_file=None):
        if sources_in_file is None:
            sources_in_file = sources
        self.sources_in_file = sources_in_file
        self.provides_sources = sources
        self.path = path
        self.data_node = data_node
        self.start = start
        self.stop = stop
        self.num_examples = self.stop - self.start
        self.nodes = None
        self.open_file(self.path)
        super(Hdf5Dataset, self).__init__(self.provides_sources)

    def open_file(self, path):
        h5file = tables.open_file(path, mode="r")
        node = h5file.getNode('/', self.data_node)

        self.nodes = [getattr(node, source) for source in self.sources_in_file]

    def load(self):
        self.open_file(self.path)

    def get_data(self, state=None, request=None):
        """ Returns data from HDF5 dataset.

        .. note:: The best performance if `request` is a slice.

        """
        if self.start:
            if isinstance(request, slice):
                request = slice(request.start + self.start,
                                request.stop + self.start, request.step)
            elif isinstance(request, list):
                request = [index + self.start for index in request]
            else:
                raise ValueError
        data = [node[request] for node in self.nodes]
        return data


@do_not_pickle_attributes('data_sources')
class H5PYDataset(Dataset):
    """An h5py-fueled HDF5 dataset.

    This dataset class assumes a particular file layout:

    * Data sources reside in the root group, and their names define the
      source names.
    * The dataset is not explicitly split. Instead, splits are defined as
      attributes of the root group. They're expected to be numpy arrays of
      shape (2,), with the first element being the starting point
      (inclusive) of the split and the last element being the stopping
      point (exclusive) of the split.

    The `which_set`, `start` and `stop` parameters work together in the
    following way:

    * `which_set` is resolved first. If it is `None`, the whole dataset is
      used.
    * `start` and `stop` define a slice *within the context of*
      `which_set`.

    Parameters
    ----------
    path : str
        Path to the HDF5 file.
    which_set : str, optional
        Name of the root group attribute containing the split information.
        Defaults to `None`, in which case the whole dataset is used.
    subset : slice, optional
        A slice of data *within the context of the split* to use. Defaults
        to `None`, in which case the whole split is used. **Note:
        at the moment, `slice.step` must be either 1 or `None`.**
    load_in_memory : bool, optional
        Whether to load the data in main memory. Defaults to `False`.
    driver : str, optional
        Low-level driver to use. Defaults to `None`. See h5py
        documentation for a complete list of available options.

    """
    ref_counts = defaultdict(int)

    def __init__(self, path, which_set=None, subset=None, load_in_memory=False,
                 driver=None, **kwargs):
        self.path = path
        self.which_set = which_set
        self.subset = subset if subset else slice(None, None, None)
        self.load_in_memory = load_in_memory
        self.driver = driver

        super(H5PYDataset, self).__init__(**kwargs)

        self.load()

    def _get_file_id(self):
        file_id = [f for f in H5PYDataset.ref_counts.keys() if f == self.path]
        if not file_id:
            return self.path
        file_id, = file_id
        return file_id

    @property
    def provides_sources(self):
        if not hasattr(self, '_provides_sources'):
            ref = self._out_of_memory_open()
            handle = H5PYDataset.ref_counts[ref][0]
            self._provides_sources = tuple(handle.keys())
            self._out_of_memory_close(ref)
        return self._provides_sources

    def load(self):
        ref = self._out_of_memory_open()
        handle = H5PYDataset.ref_counts[ref][0]
        shapes = [data_source.shape for data_source in handle.values()]
        if any(s[0] != shapes[0][0] for s in shapes):
            raise ValueError("sources have different lengths")
        if self.subset.step not in (1, None):
            raise ValueError("subset.step must be either 1 or None")
        start, stop = (handle.attrs[self.which_set] if self.which_set
                       else (0, shapes[0][0]))
        self.subset = slice(
            start if self.subset.start is None else self.subset.start,
            stop if self.subset.stop is None else self.subset.stop,
            self.subset.step)
        self.num_examples = self.subset.stop - self.subset.start
        if self.load_in_memory:
            self.data_sources = [data_source[self.subset] for
                                 source_name, data_source in handle.items()
                                 if source_name in self.sources]
        else:
            self.data_sources = None
        self._out_of_memory_close(ref)

    def open(self):
        return None if self.load_in_memory else self._out_of_memory_open()

    def _out_of_memory_open(self):
        file_id = self._get_file_id()
        if not H5PYDataset.ref_counts[file_id]:
            # Load the file since it is not currently open
            handle = h5py.File(name=file_id, mode="r", driver=self.driver)
            H5PYDataset.ref_counts[file_id] = [handle, 1]
        else:
            handle = H5PYDataset.ref_counts[file_id][0]
        return file_id

    def close(self, state):
        if not self.load_in_memory:
            self._out_of_memory_close(state)

    def _out_of_memory_close(self, state):
        H5PYDataset.ref_counts[state][1] -= 1
        if not H5PYDataset.ref_counts[state]:
            H5PyDataset.ref_counts[state][0].close()
            del H5PYDataset.ref_counts[state]

    def get_data(self, state=None, request=None):
        if self.load_in_memory:
            return self._in_memory_get_data(state=state, request=request)
        else:
            return self._out_of_memory_get_data(state=state, request=request)

    def _in_memory_get_data(self, state=None, request=None):
        if state is not None or request is None:
            raise ValueError
        return self.filter_sources([data_source[request] for data_source
                                    in self.data_sources])

    def _out_of_memory_get_data(self, state=None, request=None):
        if isinstance(request, slice):
            request = slice(request.start + self.subset.start,
                            request.stop + self.subset.start, request.step)
        elif isinstance(request, list):
            request = [index + self.subset.start for index in request]
        else:
            raise ValueError
        handle = H5PYDataset.ref_counts[state][0]
        return self.filter_sources([data_source[request] for data_source in
                                    handle.values()])
