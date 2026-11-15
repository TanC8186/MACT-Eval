from pathlib import Path

from PIL import Image
import pytest

from tools.public_data.audit_student_behavior_case import (
    CASE_CLASSES, audit, parse_labels, prepare, sha256, xml_matches_labels,
)


def case_tree(root: Path):
    data = root / "classes_dection"
    data.mkdir(parents=True)
    (data / "data.ipynb").write_text("{}", encoding="utf-8")
    (root / "last.pth").write_bytes(b"fixture, never loaded")
    (root / "train_log.txt").write_text("historical fixture", encoding="utf-8")
    for split, color, name in (("train", "white", "40010001"), ("val", "black", "40010002")):
        images, labels = data / "images" / split, data / "labels" / split
        images.mkdir(parents=True)
        labels.mkdir(parents=True)
        Image.new("RGB", (20, 20), color).save(images / (name + ".jpg"))
        (labels / (name + ".txt")).write_text("3 0.5 0.5 0.5 0.5\n", encoding="utf-8")
    return data


@pytest.mark.parametrize("row", [
    "1 0.5 0.5 -0.1 0.2", "1 nan 0.5 0.1 0.2",
    "6 0.5 0.5 0.1 0.2", "1 0.01 0.5 0.5 0.2",
])
def test_bad_annotation_is_reported_instead_of_silently_fixed(tmp_path, row):
    path = tmp_path / "labels.txt"
    path.write_text(row + "\n", encoding="utf-8")
    rows, problems = parse_labels(path)
    assert rows == []
    assert problems[0]["row"] == row
    assert path.read_text(encoding="utf-8") == row + "\n"


def test_preparation_preserves_case_taxonomy_and_source_files(tmp_path):
    source = tmp_path / "source"
    data = case_tree(source)
    original = {p: sha256(p) for p in source.rglob("*") if p.is_file()}
    result, manifest = audit(source)
    out = tmp_path / "prepared"
    prepare(source, out, result, manifest)
    assert CASE_CLASSES[3] == "using phone"
    assert CASE_CLASSES[5] == "leaning over the table"
    assert "3: using phone" in (out / "data.yaml").read_text(encoding="utf-8")
    assert (out / "labels/train/40010001.txt").read_bytes() == (data / "labels/train/40010001.txt").read_bytes()
    assert all(sha256(p) == digest for p, digest in original.items())
    with pytest.raises(FileExistsError):
        prepare(source, out, result, manifest)


def test_invalid_box_excludes_whole_image_and_records_denominator(tmp_path):
    source = tmp_path / "source"
    data = case_tree(source)
    (data / "images/train/40010003.jpg").write_bytes((data / "images/train/40010001.jpg").read_bytes())
    (data / "labels/train/40010003.txt").write_bytes((data / "labels/train/40010001.txt").read_bytes())
    label = data / "labels/train/40010001.txt"
    label.write_text(label.read_text() + "1 0.5 0.5 -0.2 0.2\n", encoding="utf-8")
    result, manifest = audit(source)
    out = tmp_path / "prepared"
    prepare(source, out, result, manifest)
    assert not (out / "images/train/40010001.jpg").exists()
    assert result["prepared"]["excluded_whole_images"] == [{"split": "train", "image": "40010001.jpg"}]
    assert result["prepared"]["images"] == {"train": 1, "val": 1}
    assert label.read_text().endswith("1 0.5 0.5 -0.2 0.2\n")


def test_exact_cross_split_leakage_blocks_preparation(tmp_path):
    source = tmp_path / "source"
    data = case_tree(source)
    (data / "images/val/40010002.jpg").write_bytes((data / "images/train/40010001.jpg").read_bytes())
    result, manifest = audit(source)
    assert result["train_val_exact_overlap"] == 1
    with pytest.raises(ValueError, match="split overlap"):
        prepare(source, tmp_path / "prepared", result, manifest)
    assert not (tmp_path / "prepared").exists()


def test_integer_xml_rounding_is_not_misreported_as_corrupt_label(tmp_path):
    xml, label = tmp_path / "image.xml", tmp_path / "image.txt"
    xml.write_text("""<annotation><size><width>100</width><height>100</height></size>
      <object><name>using phone</name><bndbox><xmin>11</xmin><ymin>21</ymin>
      <xmax>31</xmax><ymax>41</ymax></bndbox></object></annotation>""", encoding="utf-8")
    label.write_text("3 0.201 0.301 0.2 0.2\n", encoding="utf-8")
    assert xml_matches_labels(xml, label, 100, 100)
    label.write_text("3 0.501 0.301 0.2 0.2\n", encoding="utf-8")
    assert not xml_matches_labels(xml, label, 100, 100)
