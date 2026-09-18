from pathlib import Path

from utils import compute_sha256, dedupe_by_hash, human_size, unique_path


class TestHumanSize:
    def test_bytes(self):
        assert human_size(512) == "512.0 B"

    def test_kilobytes(self):
        assert human_size(1024) == "1.0 KB"

    def test_megabytes(self):
        assert human_size(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        assert human_size(1024 ** 3) == "1.0 GB"

    def test_terabytes(self):
        assert human_size(1024 ** 4) == "1.0 TB"

    def test_zero_bytes(self):
        assert human_size(0) == "0.0 B"

    def test_boundary_just_below_1024(self):
        assert "B" in human_size(1023)
        assert "KB" not in human_size(1023)


class TestComputeSha256:
    def test_known_hash(self, tmp_path):
        import hashlib
        data = b"hello world"
        f = tmp_path / "f.bin"
        f.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert compute_sha256(f) == expected

    def test_deterministic_on_same_file(self, tmp_path):
        f = tmp_path / "f.bin"
        f.write_bytes(b"consistent content")
        assert compute_sha256(f) == compute_sha256(f)

    def test_different_content_different_hash(self, tmp_path):
        a, b = tmp_path / "a.bin", tmp_path / "b.bin"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        assert compute_sha256(a) != compute_sha256(b)

    def test_large_file_chunked_correctly(self, tmp_path):
        """File larger than the 64 KB read chunk should hash correctly."""
        import hashlib
        data = b"x" * (128 * 1024)
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert compute_sha256(f) == expected

    def test_empty_file(self, tmp_path):
        import hashlib
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert compute_sha256(f) == hashlib.sha256(b"").hexdigest()


class TestUniquePath:
    def test_nonexistent_path_returned_unchanged(self, tmp_path):
        p = tmp_path / "file.pdf"
        assert unique_path(p) == p

    def test_existing_file_gets_suffix(self, tmp_path):
        p = tmp_path / "file.pdf"
        p.touch()
        result = unique_path(p)
        assert result == tmp_path / "file_1.pdf"

    def test_collision_loop_increments(self, tmp_path):
        p = tmp_path / "file.pdf"
        p.touch()
        (tmp_path / "file_1.pdf").touch()
        result = unique_path(p)
        assert result == tmp_path / "file_2.pdf"

    def test_preserves_extension(self, tmp_path):
        p = tmp_path / "archive.tar.gz"
        p.touch()
        result = unique_path(p)
        assert result.suffix == ".gz"

    def test_no_extension(self, tmp_path):
        p = tmp_path / "README"
        p.touch()
        result = unique_path(p)
        assert result == tmp_path / "README_1"


class TestDedupeByHash:
    def test_keeps_lowest_id_per_hash(self):
        items = [
            {"id": 3, "filename": "a.pdf", "size": 100, "file_hash": "abc"},
            {"id": 1, "filename": "a_copy.pdf", "size": 100, "file_hash": "abc"},
            {"id": 2, "filename": "b.pdf", "size": 200, "file_hash": "def"},
        ]
        result = dedupe_by_hash(items)
        result_ids = {r["id"] for r in result}
        assert result_ids == {1, 2}
        kept = next(r for r in result if r["id"] == 1)
        assert kept["copy_count"] == 2

    def test_falls_back_to_filename_and_size_when_no_hash(self):
        items = [
            {"id": 5, "filename": "same.pdf", "size": 100, "file_hash": None},
            {"id": 4, "filename": "same.pdf", "size": 100, "file_hash": None},
            {"id": 6, "filename": "different.pdf", "size": 100, "file_hash": None},
        ]
        result = dedupe_by_hash(items)
        result_ids = {r["id"] for r in result}
        assert result_ids == {4, 6}

    def test_single_copy_has_count_one(self):
        items = [{"id": 1, "filename": "solo.pdf", "size": 50, "file_hash": "xyz"}]
        result = dedupe_by_hash(items)
        assert result[0]["copy_count"] == 1
