import warnings

import tensorflow as tf

from phi import struct, math
from phi.data.fluidformat import _transform_for_writing, _writing_staticshape, read_zipped_array, _slugify_filename
from phi.physics._world import StateProxy
from phi.struct.context import _unsafe
from phi.data import SceneSource, Dataset as BaseDataset

from ..data.stream import consecutive_frames, FrameSelect


def build_graph_input(obj, input_type='placeholder', frames=None, names=None):
    """
Create placeholders for tensors in the supplied state.
    :param names: structure compatible with `obj` holding the associated file names
    :param obj: struct or StateProxy
    :param input_type: 'placeholder' or 'dataset_handle'
    :param frames: Number of input frames. If not None, returns a list of input structs.
    :return:
      1. Valid state containing or derived from created placeholders or dataset handle
      2. dict mapping from placeholders to their default names (using struct.names)
    """
    if isinstance(obj, StateProxy):
        obj = obj.state
    assert struct.isstruct(obj)
    # --- Shapes and names ---
    writable_obj = _transform_for_writing(obj)
    shape = _writing_staticshape(obj)
    if names is None:
        names = struct.names(writable_obj)
        names = struct.map(_slugify_filename, names, content_type=struct.names)
    if input_type == 'placeholder':
        if frames is not None:
            with _unsafe():
                placeholders = [placeholder(shape, basename='Placeholder_frame_%d' % i) for i in range(frames)]
            graph_in = [struct.map(lambda x: x, p) for p in placeholders]  # validates fields, splits staggered tensors
            streams = struct.map(lambda x: consecutive_frames(x, frames), names, content_type='streams')
            load_dict = {placeholders[i]: struct.map(lambda x: x[i], streams, leaf_condition=lambda x: isinstance(x, tuple) and isinstance(x[0], FrameSelect), content_type='streams') for i in range(frames)}
            return graph_in, load_dict
        else:
            with _unsafe():
                placeholders = placeholder(shape)
            graph_in = struct.map(lambda x: x, placeholders)  # validates fields, splits staggered tensors
            return graph_in, {placeholders: names}
    elif input_type == 'dataset_handle':
        with _unsafe():
            dtypes = struct.dtype(writable_obj)
            dataset_nodes, iterator_handle = dataset_handle(shape, dtypes, frames=frames)
        graph_in = struct.map(lambda x: x, dataset_nodes)  # validates fields, splits staggered tensors
        shapes = struct.flatten(struct.staticshape(dataset_nodes), leaf_condition=is_static_shape)
        dtypes = struct.flatten(struct.dtype(dataset_nodes))
        return graph_in, {'names': struct.flatten(names), 'iterator_handle': iterator_handle, 'shapes': shapes, 'dtypes': dtypes, 'frames': frames}
    else:
        raise ValueError(input_type)


def load_state(state):
    warnings.warn("load_state() is deprecated, use build_graph_input() instead.")
    return build_graph_input(state)


def create_dataset(scene_sources, names, shapes, dtypes, batch_size, frames=None, shuffle=False, inner_frame_stride=1, outer_frame_stride=1, prefetch=2):
    count = 0
    datasets = []
    for source in scene_sources:
        scene = source.scene
        nested_file_list = list(scene.data_paths(source.frames(), field_names=names))
        count += _example_count(len(nested_file_list), frames, inner_frame_stride, outer_frame_stride)
        dataset = tf.data.Dataset.from_tensor_slices(nested_file_list)
        dataset = dataset.map(lambda *items: tuple(tf.py_func(_read_npy_files, items, dtypes)))
        if frames is not None:
            dataset = stacked_window(dataset, frames, outer_stride=outer_frame_stride, inner_stride=inner_frame_stride)
        datasets.append(dataset)
    dataset = concat_datasets(datasets)
    if shuffle:
        dataset = dataset.shuffle(count)
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(prefetch)
    return dataset


def _read_npy_files(items):
    data = [read_zipped_array(item.decode())[0,...] for item in items]
    data = [math.to_float(array) for array in data]
    return data


def stacked_window(dataset, size, outer_stride=1, inner_stride=1):
    """
Combines lists of input elements into windows by adding a window dimension to the dataset.
All windows have the same number of elements.
    :param dataset: TensorFlow Dataset
    :param size: number of elements in each window
    :param outer_stride: skip windows; makes the outer dataset smaller
    :param inner_stride: element-stride inside each window
    :return: Dataset
    """
    dataset = dataset.window(size, shift=outer_stride, stride=inner_stride, drop_remainder=True)
    dataset = dataset.flat_map(lambda x: x)  # convert VariantDataset (window) to batch dimension
    dataset = dataset.batch(size)
    return dataset


def concat_datasets(datasets):
    """ Creates a TensorFlow Dataset from the given ordered list of TensorFlow Datasets """
    # n-1 concatenations will lead to overflow (RecursionError) if list is too long
    # Instead, concatenate them hierarchically
    assert len(datasets) > 0
    if len(datasets) == 1:
        return datasets[0]
    else:
        center = len(datasets) // 2
        dataset_1 = concat_datasets(datasets[:center])
        dataset_2 = concat_datasets(datasets[center:])
        return tf.data.Dataset.concatenate(dataset_1, dataset_2)


def _example_count(length, frames, inner_stride, outer_stride):
    if frames is None:
        return length
    return (frames - (frames * inner_stride - 1)) // outer_stride


class Dataset(BaseDataset):
    """
Extends phi.data.Datset by TensorFlow data pipeline functions.
    """
    def __init__(self, name, sources):
        BaseDataset.__init__(self, name, sources)
        self.shuffled = False
        self.prefetch_value = 1
        self.inner_frame_stride = 1
        self.outer_frame_stride = 1
        self.batch_size = None
        self.tf_dataset = None
        self.iterator = None
        self.iterator_handle = None

    @staticmethod
    def load(directory, indices=None, name=None, max_scenes=None, assume_same_frames=True, assume_same_shapes=True, frames=None):
        base = BaseDataset.load(directory, indices=indices, name=name, max_scenes=max_scenes, assume_same_frames=assume_same_frames, assume_same_shapes=assume_same_shapes, frames=frames)
        return Dataset(base.name, base.sources)

    def shuffle(self):
        assert self.tf_dataset is None
        self.shuffled = True
        return self

    def prefetch(self, prefetch):
        assert self.tf_dataset is None
        self.prefetch_value = prefetch
        return self

    def batch(self, batch_size):
        assert self.tf_dataset is None
        self.batch_size = batch_size
        return self

    def setup(self, names, shapes, dtypes, batch_size=None, frames=None):
        for source in self.sources:
            assert isinstance(source, SceneSource)
        batch_size = batch_size if batch_size is not None else self.batch_size
        batch_size = 1 if batch_size is None else batch_size
        self.tf_dataset = create_dataset(self.sources, names=names, shapes=shapes, dtypes=dtypes, batch_size=batch_size, frames=frames, shuffle=self.shuffled, inner_frame_stride=self.inner_frame_stride, outer_frame_stride=self.outer_frame_stride, prefetch=self.prefetch_value)
        self.iterator = self.tf_dataset.make_initializable_iterator()

    def reset_iterator(self, session):
        if self.iterator_handle is None:
            self.iterator_handle = session.run(self.iterator.string_handle())
        session.run(self.iterator.initializer)

    def get_reset_handle(self, session):
        self.reset_iterator(session)
        return self.iterator_handle



class DataLoader:

    def __init__(self):
        pass

    def set_data(self, dict, train=None, val=None):
        """
Specify what data to use for training and validation.

The content of `dict` determines the data pipeline that is used.
  - 'placeholder' pipline: the static TensorFlow graph uses placeholders as input. `dict` maps from placeholders to file names or Stream instances. Placeholders and corresponding streams may be placed inside structs.
  - 'dataset_handle' pipeline: Use TensorFlow data pipeline. `dict` contains 'iterator_handle' and related properties as returned by `build_graph_input(...)[1]`.

Regardless of pipeline, the recommended way to obtain `dict` is through `build_graph_input(...)[1]`.

        :param dict: pipeline-dependent dict
        :type dict: dict
        :param train: (optional) Dataset used for training
        :type train: Dataset
        :param val: (optional) Dataset used for validation
        :type val: Dataset
        """
        assert isinstance(train, Dataset) or train is None
        assert isinstance(val, Dataset) or train is None
        if train is not None or val is not None:
            assert dict is not None
        if train is not None and val is not None:
            self.value_view_training_data = False
        self._training_set = train
        self._validation_set = val
        if dict is not None and 'iterator_handle' in dict:
            self._init_tf_pipeline(**dict)
        else:
            self._init_numpy_iterators(dict)

    def _init_numpy_iterators(self, dict):
        self._pipeline = 'placeholder'
        self._placeholder_struct = []
        self._channel_struct = []
        if dict is not None:
            for key, value in dict.items():
                self._placeholder_struct.append(key)
                self._channel_struct.append(value)
        self._channel_struct = tuple(self._channel_struct)
        self._placeholder_struct = tuple(self._placeholder_struct)
        # Train
        if self._training_set is not None:
            self._train_reader = BatchReader(self._training_set, self._channel_struct)
            self._train_iterator = self._train_reader.all_batches(batch_size=self.training_batch_size, loop=True)
        else:
            self._train_reader = None
            self._train_iterator = None
        # Val
        if self._validation_set is not None:
            self._val_reader = BatchReader(self._validation_set, self._channel_struct)
        else:
            self._val_reader = None

    def _init_tf_pipeline(self, iterator_handle, names, shapes, dtypes, frames):
        self._placeholder_struct = iterator_handle
        self._pipeline = 'dataset_handle'
        if self._training_set is not None:
            train_dataset = create_dataset(self._training_set.sources, names, shapes, dtypes, batch_size=self.training_batch_size, shuffle=True, frames=frames)
            self._train_iterator = train_dataset.make_initializable_iterator()
            self._train_iterator_handle = self.session.run(self._train_iterator.string_handle())
            self.session.run(self._train_iterator.initializer)
        if self._validation_set is not None:
            val_dataset = create_dataset(self._validation_set.sources, names, shapes, dtypes, batch_size=self.validation_batch_size, shuffle=True, frames=frames)
            self._val_iterator = val_dataset.make_initializable_iterator()
            self._val_iterator_handle = self.session.run(self._val_iterator.string_handle())
