import json
from pathlib import Path

from PIL import Image
import pytest

from tools.public_data.prepare_classroom_case8 import digest, prepare


def image_with_label(root, split, name, color, labels):
    image = root / 'images' / split / (name + '.jpg')
    label = root / 'labels' / split / (name + '.txt')
    image.parent.mkdir(parents=True, exist_ok=True)
    label.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (20, 20), color).save(image)
    label.write_text(labels, encoding='utf-8')
    return image, label


def inputs(tmp_path):
    scb, case = tmp_path / 'scb', tmp_path / 'case'
    image_with_label(scb, 'train', 'scbtrain', 'white', '3 0.5 0.5 0.4 0.4\n')
    image_with_label(scb, 'val', 'scbval', 'black', '5 0.5 0.5 0.4 0.4\n')
    manifest = []
    for split, name, color, labels in (
        ('train', '40010001', 'red', '3 0.5 0.5 0.4 0.4\n5 0.3 0.3 0.2 0.2\n'),
        ('val', '40020001', 'green', '4 0.5 0.5 0.4 0.4\n'),
        ('train', '40030001', 'blue', '0 0.5 0.5 0.4 0.4\n'),
    ):
        image, label = image_with_label(case, split, name, color, labels)
        manifest.append({'eligible': True, 'split': split, 'image': image.name,
                         'image_sha256': digest(image), 'label_sha256': digest(label)})
    (case / 'file-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (case / 'audit.json').write_text(json.dumps({'prepared': {'excluded_whole_images': []}}), encoding='utf-8')
    return scb, case


def test_case_classes_are_remapped_and_temporal_groups_stay_together(tmp_path):
    scb, case = inputs(tmp_path)
    report = prepare(scb, case, tmp_path / 'combined')
    out = tmp_path / 'combined'
    assert (out / 'labels/train/case11_40010001.txt').read_text().splitlines() == [
        '6 0.5 0.5 0.4 0.4', '7 0.3 0.3 0.2 0.2']
    assert (out / 'labels/train/scb5_scbtrain.txt').read_text().startswith('3 ')
    assert (out / 'labels/val/scb5_scbval.txt').read_text().startswith('5 ')
    assert (out / 'labels/train/case11_40020001.txt').read_text().startswith('4 ')
    assert (out / 'images/val/case11_40030001.jpg').is_file()
    assert not (out / 'images/train/case11_40030001.jpg').exists()
    assert report['images_by_source_split'] == {'scb5/train': 1, 'scb5/val': 1, 'case11/train': 2, 'case11/val': 1}
    assert '6: using phone' in (out / 'data.yaml').read_text()
    assert len((out / 'case11_val.txt').read_text().splitlines()) == 1


def test_bad_scb_image_is_explicitly_excluded_and_source_is_preserved(tmp_path):
    scb, case = inputs(tmp_path)
    image, label = image_with_label(scb, 'train', 'bad', 'yellow', '2 0.5 0.5 0 0.2\n')
    before = digest(label)
    report = prepare(scb, case, tmp_path / 'combined')
    assert report['excluded_whole_images'][0]['image'] == image.name
    assert not (tmp_path / 'combined/images/train/scb5_bad.jpg').exists()
    assert digest(label) == before


def test_cross_source_duplicate_fails_before_writing(tmp_path):
    scb, case = inputs(tmp_path)
    target = scb / 'images/val/scbval.jpg'
    target.write_bytes((case / 'images/train/40010001.jpg').read_bytes())
    with pytest.raises(ValueError, match='duplicate image'):
        prepare(scb, case, tmp_path / 'combined')
    assert not (tmp_path / 'combined').exists()


def test_changed_case_label_cannot_bypass_audited_manifest(tmp_path):
    scb, case = inputs(tmp_path)
    (case / 'labels/train/40010001.txt').write_text('1 0.5 0.5 0.2 0.2\n')
    with pytest.raises(ValueError, match='differs from audited'):
        prepare(scb, case, tmp_path / 'combined')


def test_source_edge_coordinates_remain_visible_in_report_and_are_not_rewritten(tmp_path):
    scb, case = inputs(tmp_path)
    row = '2 0.01 0.5 0.2 0.2\n'
    image_with_label(scb, 'train', 'edge', 'yellow', row)
    report = prepare(scb, case, tmp_path / 'combined')
    assert report['edge_coordinate_warnings'][0]['image'] == 'edge.jpg'
    assert (tmp_path / 'combined/labels/train/scb5_edge.txt').read_text() == row


def test_existing_destination_is_never_overwritten(tmp_path):
    scb, case = inputs(tmp_path)
    out = tmp_path / 'combined'
    out.mkdir()
    sentinel = out / 'keep.txt'
    sentinel.write_text('keep')
    with pytest.raises(FileExistsError):
        prepare(scb, case, out)
    assert sentinel.read_text() == 'keep'
