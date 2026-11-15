package com.macteval.service;

import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * SEC-03 回归：直传媒体的服务端类型/大小策略。
 *
 * <p>审计当时的具体缺口是"init 请求没有 sizeBytes/contentType 字段、
 * confirm 只 statObject 存在性"。本测试固定住策略层本身的行为；
 * 端到端"绕过前端直接调 API"的证据由
 * {@code scripts/verify/upload_bypass_test.py} 在真实栈上产生。</p>
 */
class MediaUploadPolicyTest {

    private static MediaUploadPolicy policy(boolean allowOctetStream) {
        MediaUploadPolicy p = new MediaUploadPolicy();
        ReflectionTestUtils.setField(p, "maxBytes", 2L * 1024 * 1024 * 1024);
        ReflectionTestUtils.setField(p, "maxBytesPerLesson", 8L * 1024 * 1024 * 1024);
        ReflectionTestUtils.setField(p, "allowOctetStream", allowOctetStream);
        return p;
    }

    @Test
    void extensionIsLowercasedAndStripsDirectories() {
        assertEquals("mp4", MediaUploadPolicy.extensionOf("lesson.MP4"));
        assertEquals("mp4", MediaUploadPolicy.extensionOf("C:\\path\\to\\lesson.mp4"));
        assertEquals("mp4", MediaUploadPolicy.extensionOf("../../etc/lesson.mp4"));
        // 无扩展名 / 以点结尾都要安全地返回空串，而不是抛异常。
        assertEquals("", MediaUploadPolicy.extensionOf("lesson"));
        assertEquals("", MediaUploadPolicy.extensionOf("lesson."));
        assertEquals("", MediaUploadPolicy.extensionOf(null));
    }

    @Test
    void videoAndAudioWhitelistsDoNotOverlapInTheWrongDirection() {
        MediaUploadPolicy p = policy(false);
        assertTrue(p.isAllowedExtension("video", "mp4"));
        assertTrue(p.isAllowedExtension("video", "mkv"));
        assertFalse(p.isAllowedExtension("video", "mp3"));
        assertTrue(p.isAllowedExtension("audio", "mp3"));
        assertFalse(p.isAllowedExtension("audio", "mp4"));
        // exe 在两侧都不允许：这是最容易被改名绕过的一类。
        assertFalse(p.isAllowedExtension("video", "exe"));
        assertFalse(p.isAllowedExtension("audio", "exe"));
        // 未知 mediaType 一律拒绝，而不是回退到"允许"。
        assertFalse(p.isAllowedExtension("document", "pdf"));
    }

    @Test
    void contentTypeMustMatchTheExtension() {
        MediaUploadPolicy p = policy(false);
        assertTrue(p.isAllowedContentType("video", "mp4", "video/mp4"));
        // 浏览器常带 codecs 参数，必须截掉后再比较。
        assertTrue(p.isAllowedContentType("video", "mp4", "video/mp4;codecs=avc1"));
        assertTrue(p.isAllowedContentType("video", "mkv", "video/x-matroska"));
        assertTrue(p.isAllowedContentType("audio", "m4a", "audio/mp4"));
        // 扩展名与 MIME 错配
        assertFalse(p.isAllowedContentType("audio", "mp4", "audio/mpeg"));
        assertFalse(p.isAllowedContentType("video", "mp4", "text/html"));
        // HTML 是最危险的一类：私有桶里的 HTML 一旦被当页面打开就是存储型 XSS。
        assertFalse(p.isAllowedContentType("video", "mp4", "application/xhtml+xml"));
        assertFalse(p.isAllowedContentType("video", "mp4", null));
        assertFalse(p.isAllowedContentType("video", "mp4", "  "));
    }

    @Test
    void octetStreamIsRejectedUnlessExplicitlyEnabled() {
        assertFalse(policy(false).isAllowedContentType("video", "mp4", "application/octet-stream"));
        assertTrue(policy(true).isAllowedContentType("video", "mp4", "application/octet-stream"));
        // 即便开启，扩展名白名单仍然生效——octet-stream 只放宽 MIME，不放宽扩展名。
        assertFalse(policy(true).isAllowedExtension("video", "exe"));
    }

    @Test
    void configuredLimitsAreExposedToCallers() {
        MediaUploadPolicy p = policy(false);
        assertEquals(2L * 1024 * 1024 * 1024, p.maxBytes());
        assertEquals(8L * 1024 * 1024 * 1024, p.maxBytesPerLesson());
        assertTrue(p.allowedExtensions("video").contains("mp4"));
        assertTrue(p.allowedExtensions("audio").contains("wav"));
        assertEquals(0, p.allowedExtensions("unknown").size());
    }
}
