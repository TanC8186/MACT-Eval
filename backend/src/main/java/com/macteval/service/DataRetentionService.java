package com.macteval.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.time.Instant;
import java.util.stream.Stream;

/**
 * 数据保留清理服务。
 *
 * <p>教育场景下，课堂原始媒体（人脸 / 课堂视频）属于个人信息，应遵循
 * 最小化保存原则。本服务按配置的保留期定期清理本地上传目录中过期的
 * 原始媒体文件，满足"数据保留期限"的隐私合规要求。
 *
 * <p>默认每天 03:00 运行一次（可通过 {@code mact.privacy.cleanup-cron} 覆盖），
 * 保留期默认 90 天（可通过 {@code mact.privacy.retention-days} 覆盖）。
 * 每次删除记录 文件路径 + 大小 + 删除时间 的审计日志。
 */
@Slf4j
@Service
public class DataRetentionService {

    private final long retentionDays;
    private final String uploadDir;

    public DataRetentionService(
            @Value("${mact.privacy.retention-days:90}") long retentionDays,
            @Value("${mact.privacy.upload-dir:data/uploads}") String uploadDir) {
        this.retentionDays = retentionDays;
        this.uploadDir = uploadDir;
    }

    /**
     * 定时清理超过保留期的本地上传文件。
     */
    @Scheduled(cron = "${mact.privacy.cleanup-cron:0 0 3 * * *}")
    public void cleanupExpiredUploads() {
        Path dir = Paths.get(uploadDir);
        if (!Files.isDirectory(dir)) {
            log.debug("数据保留清理跳过：上传目录不存在 {}", dir.toAbsolutePath());
            return;
        }

        Instant cutoff = Instant.now().minus(Duration.ofDays(retentionDays));
        log.info("数据保留清理开始: dir={}, retentionDays={}, cutoff={}",
                dir.toAbsolutePath(), retentionDays, cutoff);

        int deletedCount = 0;
        long freedBytes = 0;

        try (Stream<Path> files = Files.list(dir)) {
            for (Path file : files.toList()) {
                if (Files.isDirectory(file)) {
                    continue;
                }
                try {
                    Instant lastModified = Files.getLastModifiedTime(file).toInstant();
                    if (lastModified.isBefore(cutoff)) {
                        long size = Files.size(file);
                        if (Files.deleteIfExists(file)) {
                            deletedCount++;
                            freedBytes += size;
                            // 审计日志：路径 + 大小 + 删除时间
                            log.info("RETENTION_DELETE path={}, size={} bytes, deletedAt={}",
                                    file.toAbsolutePath(), size, Instant.now());
                        }
                    }
                } catch (IOException e) {
                    log.warn("数据保留清理处理文件失败: {}", file.toAbsolutePath(), e);
                }
            }
        } catch (IOException e) {
            log.error("数据保留清理目录遍历失败: {}", dir.toAbsolutePath(), e);
        }

        log.info("数据保留清理完成: deletedFiles={}, freedBytes={}", deletedCount, freedBytes);
    }

    /**
     * 删除指定本地上传文件（供授权撤回 / 主动删除等隐私操作调用）。
     *
     * @param objectName 上传目录内的文件名（由服务端传入，不应来自用户自由输入）
     * @return 是否成功删除
     */
    public boolean deleteUpload(String objectName) {
        if (objectName == null || objectName.isBlank()) {
            return false;
        }
        Path baseDir = Paths.get(uploadDir).toAbsolutePath().normalize();
        Path file = baseDir.resolve(objectName).normalize();
        if (!file.startsWith(baseDir)) {
            log.warn("拒绝删除上传目录之外的文件: {}", file.toAbsolutePath());
            return false;
        }
        try {
            long size = Files.size(file);
            boolean removed = Files.deleteIfExists(file);
            if (removed) {
                log.info("PRIVACY_DELETE path={}, size={} bytes, deletedAt={}",
                        file.toAbsolutePath(), size, Instant.now());
            }
            return removed;
        } catch (IOException e) {
            log.warn("删除上传文件失败: {}", file.toAbsolutePath(), e);
            return false;
        }
    }
}
