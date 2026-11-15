package com.macteval.service;

import jakarta.annotation.PreDestroy;
import lombok.Getter;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * AI 模型管理器 — 占位实现（placeholder），真实模型加载尚未完成。
 * <p>
 * 本类是未来 YOLO / Whisper / BERT 模型加载的占位管理器，仅维护路径与就绪标志。
 * <b>{@link #loadYolo()} / {@link #loadWhisper()} / {@link #loadBert()} 当前一律返回
 * {@code false}（表示「未实现」，不是加载成功）</b>，只记录 WARN 日志，不做任何实际加载。
 * <p>
 * 当前真实的 AI 能力由 ai-service（FastAPI + OpenCV）通过 {@link AiServiceClient}
 * 提供，与本类无关。正式评价管线接通前，本类不加载任何模型。
 */
@Service
@Slf4j
public class ModelManager {

    @Value("${mact.model.dir:../models}")
    private String modelDir;

    @Getter
    private Path modelPath;

    private final AtomicBoolean yoloReady = new AtomicBoolean(false);
    private final AtomicBoolean whisperReady = new AtomicBoolean(false);
    private final AtomicBoolean bertReady = new AtomicBoolean(false);

    @PostConstruct
    public void init() {
        modelPath = Path.of(modelDir).toAbsolutePath();
        log.info("ModelManager initialized. Model directory: {}", modelPath);

        if (!Files.exists(modelPath)) {
            log.warn("Model directory does not exist. Creating: {}", modelPath);
            modelPath.toFile().mkdirs();
        }
    }

    /**
     * 加载 YOLO 人脸检测模型。
     * <p>
     * 注意：<b>当前未实现</b>，返回 false 表示「未实现 / 未加载」，不代表加载成功。
     * 规划中从 modelPath/yolo/ 加载 ONNX 权重，当前仅为占位。
     */
    public boolean loadYolo() {
        if (yoloReady.get()) return true;
        Path yoloPath = modelPath.resolve("yolo");
        if (Files.exists(yoloPath)) {
            log.warn("YOLO weights found at {}, but the runtime loader is not implemented", yoloPath);
        } else {
            log.warn("YOLO model not found at {}", yoloPath);
        }
        return false;
    }

    /**
     * 加载 Whisper ASR 模型。
     * <p>
     * 注意：<b>当前未实现</b>，返回 false 表示「未实现 / 未加载」，不代表加载成功。
     */
    public boolean loadWhisper() {
        if (whisperReady.get()) return true;
        log.warn("Whisper runtime loader is not implemented");
        return false;
    }

    /**
     * 加载 BERT 文本编码模型。
     * <p>
     * 注意：<b>当前未实现</b>，返回 false 表示「未实现 / 未加载」，不代表加载成功。
     */
    public boolean loadBert() {
        if (bertReady.get()) return true;
        log.warn("BERT runtime loader is not implemented");
        return false;
    }

    public boolean isYoloReady() { return yoloReady.get(); }
    public boolean isWhisperReady() { return whisperReady.get(); }
    public boolean isBertReady() { return bertReady.get(); }

    @PreDestroy
    public void cleanup() {
        log.info("Releasing model resources...");
        yoloReady.set(false);
        whisperReady.set(false);
        bertReady.set(false);
    }
}
