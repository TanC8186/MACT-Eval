package com.macteval.service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.time.Instant;
import java.time.temporal.ChronoUnit;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 数据保留清理服务单元测试。
 * 验证超过保留期的上传文件被清理、保留期内的文件被保留。
 */
class DataRetentionServiceTest {

    @TempDir
    Path tempDir;

    private DataRetentionService newService(long retentionDays) {
        return new DataRetentionService(retentionDays, tempDir.toString());
    }

    @Test
    @DisplayName("超过保留期的文件应被删除")
    void expiredFiles_areDeleted() throws IOException {
        Path oldFile = tempDir.resolve("upload_old.bin");
        Files.write(oldFile, new byte[1024]);
        Files.setLastModifiedTime(oldFile,
                FileTime.from(Instant.now().minus(100, ChronoUnit.DAYS)));

        newService(90).cleanupExpiredUploads();

        assertFalse(Files.exists(oldFile), "超过保留期的文件应被删除");
    }

    @Test
    @DisplayName("保留期内的文件应被保留")
    void recentFiles_areKept() throws IOException {
        Path recentFile = tempDir.resolve("upload_recent.bin");
        Files.write(recentFile, new byte[1024]);

        newService(90).cleanupExpiredUploads();

        assertTrue(Files.exists(recentFile), "保留期内的文件应被保留");
    }

    @Test
    @DisplayName("不存在的上传目录应安全跳过而不抛异常")
    void missingUploadDir_isSkipped() {
        DataRetentionService service = new DataRetentionService(90, tempDir.resolve("nope").toString());
        assertDoesNotThrow(service::cleanupExpiredUploads);
    }

    @Test
    @DisplayName("deleteUpload 应删除指定文件并返回 true")
    void deleteUpload_removesFile() throws IOException {
        Path file = tempDir.resolve("upload_target.bin");
        Files.write(file, new byte[512]);

        DataRetentionService service = newService(90);
        boolean removed = service.deleteUpload("upload_target.bin");

        assertTrue(removed);
        assertFalse(Files.exists(file));
    }

    @Test
    @DisplayName("deleteUpload 拒绝删除上传目录之外的路径")
    void deleteUpload_rejectsOutsideDir() throws IOException {
        Path outside = Files.createTempFile("outside", ".bin");

        DataRetentionService service = newService(90);
        boolean removed = service.deleteUpload(outside.toAbsolutePath().toString());

        assertFalse(removed);
        assertTrue(Files.exists(outside));
    }
}
