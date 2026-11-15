package com.macteval.dto;

/**
 * 供教师人工复核的识别证据帧。
 *
 * @param url 短期签名下载 URL；对象桶为私有，前端不得拼接固定地址
 */
public record EvidenceFrameDTO(
        String id,
        Integer windowIndex,
        Long timestampMs,
        String code,
        String label,
        String url,
        java.util.Map<String, Object> metadata
) {
    public EvidenceFrameDTO(String id, Integer windowIndex, Long timestampMs,
                            String code, String label, String url) {
        this(id, windowIndex, timestampMs, code, label, url, java.util.Map.of());
    }
}
