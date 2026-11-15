package com.macteval.service;

import io.minio.MinioClient;
import io.minio.http.Method;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 验证预签名 URL 由「浏览器可达 endpoint」的独立客户端生成（签名按 public host 计算），
 * 内部 I/O（objectExists）仍走内网客户端。
 *
 * <p>若直接用内网端点（minio:9000）生成 URL 再重写主机名，SigV4 签名会因 host 不匹配而失效（403）。</p>
 */
class MinioServicePresignedUrlTest {

    private MinioClient internalClient;
    private MinioClient presignClient;
    private MinioService service;

    @BeforeEach
    void setUp() {
        internalClient = mock(MinioClient.class);
        presignClient = mock(MinioClient.class);
        service = new MinioService(internalClient, presignClient);
    }

    @Test
    void generateUploadUrlUsesPresignClient() throws Exception {
        String publicUrl = "http://localhost:9000/classroom-raw-media/abc.mp4?X-Amz-Signature=deadbeef";
        when(presignClient.getPresignedObjectUrl(any())).thenReturn(publicUrl);

        String url = service.generateUploadUrl("classroom-raw-media", "abc.mp4", 30);

        assertEquals(publicUrl, url);
        // 预签名必须用 presign 客户端，且不得直接使用内网客户端生成
        verify(presignClient).getPresignedObjectUrl(any());
        verify(internalClient, never()).getPresignedObjectUrl(any());
    }

    @Test
    void generateDownloadUrlUsesPresignClient() throws Exception {
        String publicUrl = "http://localhost:9000/teaching-resources/deck.pdf?X-Amz-Signature=aa";
        when(presignClient.getPresignedObjectUrl(any())).thenReturn(publicUrl);

        String url = service.generateDownloadUrl("teaching-resources", "deck.pdf", 30);

        assertEquals(publicUrl, url);
        verify(presignClient).getPresignedObjectUrl(any());
        verify(internalClient, never()).getPresignedObjectUrl(any());
    }

    @Test
    void objectExistsUsesInternalClient() throws Exception {
        when(internalClient.statObject(any())).thenReturn(mock(io.minio.StatObjectResponse.class));

        boolean exists = service.objectExists("classroom-raw-media", "abc.mp4");

        assertTrue(exists);
        verify(internalClient).statObject(any());
    }
}