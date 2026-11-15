package com.macteval.service;

import com.macteval.dto.MediaUploadConfirmRequest;
import com.macteval.dto.MediaUploadInitRequest;
import com.macteval.exception.BusinessException;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.MediaAssetEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.MediaAssetRepository;
import com.macteval.security.ResourceGuard;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.file.Paths;
import java.time.Instant;
import java.time.ZoneId;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.UUID;

/**
 * 媒体资产生命周期：上传会话、归属校验、配额与到期清理。
 *
 * <p><b>服务端上传校验（SEC-03，2026-10-28）</b>：直传的文件不经过 Spring，
 * 所以 2 GB 的 multipart 上限对这条路径完全不生效。本类因此在两个阶段各做一次
 * 校验，且两次都必须通过：</p>
 *
 * <ol>
 *   <li><b>init</b>：客户端必须声明 {@code sizeBytes} 与 {@code contentType}，
 *       服务端立即核对扩展名、MIME 与单文件上限。声明值本身不阻止写入
 *       （MinIO 的预签名 PUT 无法携带 content-length-range），但它把"预期"
 *       记进了上传会话，使 confirm 能发现不一致。</li>
 *   <li><b>confirm</b>：用 {@code statObject} 的**实测**大小与 MIME 再校验一次，
 *       并核对与 init 声明是否一致。任何一条不通过 → 删除对象 → 4xx，
 *       不创建分析任务。</li>
 * </ol>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class MediaAssetService {

    public static final String MEDIA_BUCKET = "classroom-raw-media";
    /**
     * 临时上传桶与前缀（SEC-04）。该桶是唯一没有 DB 记录的对象存放地，
     * 因此清理只能靠"按前缀列举 + 按对象写入时间判断"，不能靠资产表。
     */
    public static final String TEMPORARY_BUCKET = "temporary-uploads";
    public static final String TEMPORARY_PREFIX = "temp/";
    private static final List<String> ACTIVE_STATUSES =
            List.of("PENDING", "UPLOADED", "PROCESSING");

    private final MediaAssetRepository assetRepository;
    private final BehaviorEvidenceFrameRepository evidenceFrameRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final ResourceGuard resourceGuard;
    private final MinioService minioService;
    private final MediaUploadService mediaUploadService;
    private final MediaUploadPolicy uploadPolicy;

    @Value("${mact.media.retention-days:90}")
    private long retentionDays;

    @Value("${mact.media.max-assets-per-lesson:20}")
    private long maxAssetsPerLesson;

    /**
     * 临时上传对象的保留小时数。
     *
     * <p>为什么是 24 而不是 90（媒体资产的保留期）：临时对象没有 DB 行、
     * 没有分析任务、没有任何东西会再读它。它的唯一用途是"先落盘再交给
     * 别处"，过一天还没被用掉就是垃圾。用 90 天等于给这个无人管理的
     * 桶开 90 天的无限写入窗口。</p>
     */
    @Value("${mact.upload.temporary-retention-hours:24}")
    private long temporaryRetentionHours;

    @Transactional
    public Map<String, String> createUploadSession(MediaUploadInitRequest request, String ownerId) {
        LessonSessionEntity session = lessonSessionRepository.findById(request.lessonSessionId())
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(ownerId, session.getTeacherId());

        long active = assetRepository.countByLessonSessionIdAndStatusIn(
                request.lessonSessionId(), ACTIVE_STATUSES);
        if (active >= maxAssetsPerLesson) {
            throw new BusinessException(429,
                    "该课堂媒体资产数量已达上限（" + maxAssetsPerLesson + "），请先清理旧媒体");
        }

        String safeName = Paths.get(request.fileName()).getFileName().toString();
        String extension = MediaUploadPolicy.extensionOf(safeName);

        // --- 校验 1：扩展名白名单（按媒体类型分开） ---
        if (!uploadPolicy.isAllowedExtension(request.mediaType(), extension)) {
            throw new BusinessException(415,
                    "不支持的文件类型 ." + extension + "；" + request.mediaType()
                            + " 仅允许 " + String.join("/", uploadPolicy.allowedExtensions(request.mediaType())));
        }
        // --- 校验 2：声明的 MIME 必须与该扩展名匹配 ---
        // 只查扩展名不够：把 a.exe 改名成 a.mp4 也能过，但浏览器会报 video/mp4
        // 还是 application/octet-stream 取决于调用方。声明与扩展名对不上时
        // 直接拒绝，而不是等到 confirm 才发现。
        if (!uploadPolicy.isAllowedContentType(
                request.mediaType(), extension, request.contentType())) {
            throw new BusinessException(415,
                    "声明的 Content-Type（" + request.contentType() + "）与扩展名 ."
                            + extension + " 不匹配");
        }
        // --- 校验 3：声明大小在服务端上限内 ---
        if (request.sizeBytes() <= 0 || request.sizeBytes() > uploadPolicy.maxBytes()) {
            throw new BusinessException(413,
                    "声明的文件大小 " + request.sizeBytes() + " 字节超出上限 "
                            + uploadPolicy.maxBytes() + " 字节");
        }
        // --- 校验 4：单课堂累计容量 ---
        long alreadyDeclared = declaredBytesForLesson(request.lessonSessionId());
        if (alreadyDeclared + request.sizeBytes() > uploadPolicy.maxBytesPerLesson()) {
            throw new BusinessException(413,
                    "该课堂媒体总容量将超过上限 " + uploadPolicy.maxBytesPerLesson()
                            + " 字节（已占用 " + alreadyDeclared + " 字节）");
        }

        String fileId = UUID.randomUUID().toString();
        String objectName = fileId + "_" + safeName;

        MediaAssetEntity asset = new MediaAssetEntity();
        asset.setId(UUID.randomUUID().toString());
        asset.setOwnerId(ownerId);
        asset.setLessonSessionId(request.lessonSessionId());
        asset.setBucket(MEDIA_BUCKET);
        asset.setObjectKey(objectName);
        asset.setOriginalFileName(safeName);
        asset.setMediaType(request.mediaType());
        // 声明值先落库，confirm 时用实测值覆盖；两者不一致会被拒绝，
        // 因此这个字段不是"最终大小"，是"预期大小"。
        asset.setSizeBytes(request.sizeBytes());
        asset.setContentType(request.contentType());
        asset.setStatus(MediaAssetEntity.Status.PENDING.name());
        asset.setExpiresAt(LocalDateTime.now().plusDays(retentionDays));
        assetRepository.save(asset);

        return Map.of(
                "assetId", asset.getId(),
                "fileId", fileId,
                "objectName", objectName,
                "uploadUrl", minioService.getMediaUploadUrl(objectName),
                "mediaType", request.mediaType(),
                "maxBytes", String.valueOf(uploadPolicy.maxBytes())
        );
    }

    /**
     * 确认上传：只允许资产属主确认同一 objectKey；确认后才创建分析任务。
     *
     * <p>顺序刻意是"先校验、后落库、最后才建任务"：任何一步失败都不会留下
     * 一个指向已经被删除对象的 PROCESSING 资产。具体地——</p>
     *
     * <ul>
     *   <li>校验失败时调用 {@code removeObject}，然后抛 4xx。异常会让事务回滚，
     *       但**对象删除不会回滚**（MinIO 不在事务里）。这正好是我们要的方向：
     *       宁可留下一条 PENDING 的资产记录（下次 cleanup 会清掉），
     *       也不留下一个已写入的超大对象。</li>
     *   <li>反过来（先建任务再校验）会产生"任务已排队、对象已被删"的僵尸任务，
     *       worker 反复取不到文件——那正是本轮要避免的状态。</li>
     * </ul>
     */
    @Transactional
    public Map<String, String> confirmUpload(MediaUploadConfirmRequest request, String ownerId) {
        MediaAssetEntity asset = assetRepository.findByIdAndOwnerId(request.assetId(), ownerId)
                .orElseThrow(() -> new NoSuchElementException("上传会话不存在或不属于当前用户"));
        if (!asset.getLessonSessionId().equals(request.lessonSessionId())) {
            throw new BusinessException(409, "上传会话与课堂不一致");
        }
        if (!asset.getObjectKey().equals(request.objectName())) {
            throw new BusinessException(409, "对象键与上传会话不一致");
        }
        if ("EXPIRED".equals(asset.getStatus()) || "DELETED".equals(asset.getStatus())) {
            throw new BusinessException(409, "上传会话已过期，请重新上传");
        }

        MinioService.ObjectInfo info = minioService.statObject(
                asset.getBucket(), asset.getObjectKey());
        if (info == null) {
            throw new BusinessException(400, "媒体文件尚未上传完成，请先完成上传再发起分析");
        }

        // --- 实测大小上限 ---
        if (info.size() <= 0) {
            throw rejectObject(asset, 400, "上传的对象为空（size=" + info.size() + "）");
        }
        if (info.size() > uploadPolicy.maxBytes()) {
            throw rejectObject(asset, 413,
                    "实际文件大小 " + info.size() + " 字节超出上限 " + uploadPolicy.maxBytes() + " 字节");
        }
        // --- 实测值和 init 声明的一致性 ---
        // 容忍 1 字节误差没有意义（大小是确定的），但**允许声明值缺失的老会话**：
        // 升级前创建、升级后 confirm 的上传会话 sizeBytes 是 null，此时只按实测值校验。
        if (asset.getSizeBytes() != null && asset.getSizeBytes() > 0
                && asset.getSizeBytes() != info.size()) {
            throw rejectObject(asset, 409,
                    "实际大小 " + info.size() + " 字节与申请时声明的 "
                            + asset.getSizeBytes() + " 字节不一致");
        }
        // --- 实测 MIME ---
        // statObject 的 contentType 是**上传者自己设的请求头**，MinIO 不校验。
        // 它单独不能证明任何事，但配合扩展名白名单可以挡住"扩展名合法、MIME 离谱"
        // 的组合，也挡住把 octet-stream 当通用容器用。
        String extension = MediaUploadPolicy.extensionOf(asset.getOriginalFileName());
        if (!uploadPolicy.isAllowedExtension(asset.getMediaType(), extension)) {
            throw rejectObject(asset, 415, "扩展名 ." + extension + " 不在允许列表内");
        }
        if (info.contentType() != null && !info.contentType().isBlank()
                && !uploadPolicy.isAllowedContentType(
                        asset.getMediaType(), extension, info.contentType())) {
            throw rejectObject(asset, 415,
                    "上传对象的 Content-Type（" + info.contentType() + "）与扩展名 ."
                            + extension + " 不匹配");
        }

        asset.setSizeBytes(info.size());
        asset.setEtag(info.etag());
        asset.setContentType(info.contentType());
        asset.setStatus(MediaAssetEntity.Status.UPLOADED.name());
        asset.setExpiresAt(LocalDateTime.now().plusDays(retentionDays));
        assetRepository.save(asset);

        Map<String, String> result = mediaUploadService.triggerAnalysis(
                asset.getMediaType(),
                asset.getObjectKey(),
                asset.getOriginalFileName(),
                asset.getLessonSessionId(),
                request.totalSeats(),
                request.sampleInterval(),
                request.windowDuration(),
                request.minObservations(),
                ownerId,
                asset.getId());
        asset.setStatus(MediaAssetEntity.Status.PROCESSING.name());
        assetRepository.save(asset);
        return Map.of("jobId", result.get("jobId"),
                "status", result.get("status"),
                "assetId", asset.getId());
    }

    /**
     * 拒绝一个已写入的对象：先删除对象，再把理由交给调用方抛 4xx。
     *
     * <p>删除失败时仍然拒绝（不"因为删不掉就放行"），并打 WARN 让运维知道
     * 有一个越界对象残留——它会在 24 小时后的 PENDING 清理里被再次尝试删除。</p>
     */
    private BusinessException rejectObject(MediaAssetEntity asset, int code, String message) {
        boolean removed = minioService.removeObject(asset.getBucket(), asset.getObjectKey());
        if (removed) {
            log.warn("UPLOAD_REJECTED_REMOVED assetId={}, objectKey={}, reason={}",
                    asset.getId(), asset.getObjectKey(), message);
        } else {
            log.warn("UPLOAD_REJECTED_KEEP assetId={}, objectKey={}, reason={}; "
                            + "object removal failed, cleanup will retry",
                    asset.getId(), asset.getObjectKey(), message);
        }
        return new BusinessException(code, message);
    }

    /** 该课堂已声明但尚未过期/删除的字节合计（用于累计容量上限）。 */
    private long declaredBytesForLesson(String lessonSessionId) {
        long total = 0;
        for (MediaAssetEntity asset : assetRepository.findByLessonSessionIdAndStatusIn(
                lessonSessionId, ACTIVE_STATUSES)) {
            if (asset.getSizeBytes() != null) {
                total += asset.getSizeBytes();
            }
        }
        return total;
    }

    /**
     * 每日清理：到期资产删除对象并标记 EXPIRED；超过 24h 未确认的上传会话
     * 标记 EXPIRED（可能存在的半截对象也一并清理）。审计日志保留 objectKey。
     */
    @Scheduled(cron = "${mact.media.cleanup-cron:0 30 3 * * *}")
    @Transactional
    public void cleanupExpiredAssets() {
        LocalDateTime now = LocalDateTime.now();
        List<MediaAssetEntity> expired = assetRepository.findByStatusInAndExpiresAtBefore(
                List.of("UPLOADED", "PROCESSING"), now);
        for (MediaAssetEntity asset : expired) {
            boolean removed = minioService.removeObject(asset.getBucket(), asset.getObjectKey());
            if (!removed) {
                // 删除失败（MinIO 抖动/权限）不能标记已过期，否则下次查询不会再选中它，
                // 含学生影像的对象会永久滞留。保留原状态，下一轮继续重试。
                log.warn("RETENTION_DELETE_MEDIA_ASSET_FAILED assetId={}, objectKey={}; will retry",
                        asset.getId(), asset.getObjectKey());
                continue;
            }
            asset.setStatus(MediaAssetEntity.Status.EXPIRED.name());
            assetRepository.save(asset);
            log.info("RETENTION_DELETE_MEDIA_ASSET assetId={}, bucket={}, objectKey={}, deletedAt={}",
                    asset.getId(), asset.getBucket(), asset.getObjectKey(), now);
        }

        List<MediaAssetEntity> stalePending = assetRepository.findByStatusAndCreatedAtBefore(
                "PENDING", now.minusHours(24));
        for (MediaAssetEntity asset : stalePending) {
            boolean removed = minioService.removeObject(asset.getBucket(), asset.getObjectKey());
            if (!removed) {
                log.warn("RETENTION_EXPIRE_UPLOAD_SESSION_FAILED assetId={}, objectKey={}; will retry",
                        asset.getId(), asset.getObjectKey());
                continue;
            }
            asset.setStatus(MediaAssetEntity.Status.EXPIRED.name());
            assetRepository.save(asset);
            log.info("RETENTION_EXPIRE_UPLOAD_SESSION assetId={}, objectKey={}",
                    asset.getId(), asset.getObjectKey());
        }

        // 临时上传桶：没有资产行，只能按前缀 + 对象写入时间清理（SEC-04）。
        // 不清的话，这个桶就是一个"任何 teacher/admin 都能无限写入、永不过期、
        // 永不回收"的私有存储入口——审计里它是最干净的一条攻击面描述。
        sweepTemporaryUploads(now);

        // 识别证据帧与原始媒体同周期清理，避免课堂画面长期滞留。
        var expiredFrames = evidenceFrameRepository.findByCreatedAtBefore(
                now.minusDays(retentionDays));
        for (var frame : expiredFrames) {
            boolean removed = minioService.removeObject(frame.getBucket(), frame.getObjectKey());
            if (!removed) {
                log.warn("RETENTION_DELETE_EVIDENCE_FRAME_FAILED frameId={}, objectKey={}; will retry",
                        frame.getId(), frame.getObjectKey());
                continue;
            }
            evidenceFrameRepository.deleteById(frame.getId());
            log.info("RETENTION_DELETE_EVIDENCE_FRAME frameId={}, objectKey={}, deletedAt={}",
                    frame.getId(), frame.getObjectKey(), now);
        }
    }

    /**
     * 清扫 {@code temporary-uploads/temp/} 下超过保留期的对象。
     *
     * <p><b>为什么用对象的 {@code lastModified} 而不是对象名</b>：对象名里的
     * UUID 与文件名都是客户端提供的，可以伪造一个"看起来是昨天"的名字；
     * 而 {@code lastModified} 由存储写入，改不了。</p>
     *
     * <p><b>为什么列举失败时什么都不删</b>：{@code listObjects} 在存储不可达时
     * 返回空列表。如果把"空列表"当成"桶是空的"，日志会显示"清理完成、删除 0 个"，
     * 而真实情况是没看到任何一个对象——那是最糟的一种假绿。这里只在**确实列举
     * 到对象**时才动手，其它情况保持原样，下一轮重试。</p>
     */
    private void sweepTemporaryUploads(LocalDateTime now) {
        Instant cutoff = now.minusHours(temporaryRetentionHours)
                .atZone(ZoneId.systemDefault()).toInstant();
        List<MinioService.StoredObject> objects =
                minioService.listObjects(TEMPORARY_BUCKET, TEMPORARY_PREFIX);
        if (objects.isEmpty()) {
            // 不区分"桶空"与"列举失败"在计数上没有意义，但日志里有：listObjects
            // 失败时自己会 WARN。这里只记一条 debug，避免每天刷屏。
            log.debug("TEMPORARY_UPLOAD_SWEEP empty bucket or list failure (see WARN above)");
            return;
        }
        int deleted = 0;
        int failed = 0;
        for (MinioService.StoredObject object : objects) {
            if (object.lastModified() != null && object.lastModified().isAfter(cutoff)) {
                continue;
            }
            boolean removed = minioService.removeObject(TEMPORARY_BUCKET, object.objectName());
            if (!removed) {
                failed++;
                log.warn("RETENTION_DELETE_TEMP_UPLOAD_FAILED objectKey={}, sizeBytes={}; will retry",
                        object.objectName(), object.sizeBytes());
                continue;
            }
            deleted++;
            log.info("RETENTION_DELETE_TEMP_UPLOAD objectKey={}, sizeBytes={}, deletedAt={}",
                    object.objectName(), object.sizeBytes(), now);
        }
        if (deleted > 0 || failed > 0) {
            log.info("RETENTION_TEMP_UPLOAD_SWEEP seen={}, deleted={}, failed={}, retentionHours={}",
                    objects.size(), deleted, failed, temporaryRetentionHours);
        }
    }
}
