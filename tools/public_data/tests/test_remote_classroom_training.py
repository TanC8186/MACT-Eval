from types import SimpleNamespace

import pytest

from tools.public_data import remote_classroom_training as runner


def test_model_and_dataset_paths_cannot_escape_mact(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path / 'MACT')
    assert runner.confined(tmp_path / 'MACT/datasets/example/data.yaml').is_relative_to(tmp_path / 'MACT')
    with pytest.raises(ValueError):
        runner.confined(tmp_path / 'another_project/data.yaml')


def test_validation_uses_model_class_names_and_rejects_nan_metrics():
    box = SimpleNamespace(ap_class_index=[6, 7], class_result=lambda i: (.5, .4, .3, .2))
    result = SimpleNamespace(box=box, names={6: 'using phone', 7: 'leaning over the table'}, results_dict={'map': .3})
    summary = runner.summarize_validation(result)
    assert [r['name'] for r in summary['per_class']] == ['using phone', 'leaning over the table']
    result.results_dict['map'] = float('nan')
    with pytest.raises(ValueError, match='Non-finite'):
        runner.summarize_validation(result)


@pytest.mark.parametrize('seen,raises', [(3, False), (2, True)])
def test_validation_count_comes_from_actual_callback_and_rejects_missing_images(seen, raises):
    class Model:
        # Mirrors the callback API; deliberately has no model.validator attribute.
        def __init__(self):
            self.callbacks = {'on_val_end': []}

        def add_callback(self, event, callback):
            self.callbacks[event].append(callback)

        def val(self, **kwargs):
            for callback in self.callbacks['on_val_end']:
                callback(SimpleNamespace(seen=seen, dataloader=SimpleNamespace(dataset=[0, 1, 2])))
            return 'metrics'

    model = Model()
    if raises:
        with pytest.raises(ValueError, match='denominator mismatch'):
            runner.validate_with_count(model, data='example.yaml')
    else:
        assert runner.validate_with_count(model, data='example.yaml') == ('metrics', 3)
    assert model.callbacks['on_val_end'] == []
