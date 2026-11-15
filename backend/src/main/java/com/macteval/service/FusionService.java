package com.macteval.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * 晚期加权融合（决策级多模态融合）——范围收窄后仅保留 video/speech 两模态。
 * <p>
 * 各模态独立产出窗口级评分（0-100），按权重加权求和：total = Σ wᵢ·sᵢ。
 * 缺失模态以中性值 50 填充；两模态皆缺失返回 null。
 * 权重来源由 {@code mact.fusion.weight-source} 决定：
 * <ul>
 *   <li>{@link WeightSource#EQUAL}：等权 0.5/0.5（默认，保持兼容）；</li>
 *   <li>{@link WeightSource#PRIOR}：理论先验，默认 0.6/0.4（视觉线索主导敏感性先验）；</li>
 *   <li>{@link WeightSource#CONFIG}：显式配置权重，归一化后使用；</li>
 *   <li>{@link WeightSource#LEARNED}：离线实验学习得到的权重经配置注入，归一化后使用。</li>
 * </ul>
 * 权重学习（W3/W4）在 Python 离线实验中完成，Java 运行时只消费权重。
 *
 * @see <a href="docs/experiments/fusion-weight-protocol.md">融合权重协议</a>
 */
@Service
@Slf4j
public class FusionService {

    /** 缺失模态的中性填充值 */
    public static final double NEUTRAL_SCORE = 50.0;

    public static final String VIDEO = "video";
    public static final String SPEECH = "speech";

    /** 理论先验默认权重（视觉线索主导敏感性先验）。 */
    public static final double DEFAULT_PRIOR_VIDEO_WEIGHT = 0.6;
    public static final double DEFAULT_PRIOR_SPEECH_WEIGHT = 0.4;

    private final WeightSource weightSource;
    private final Double priorVideoWeight;
    private final Double priorSpeechWeight;
    private final Double videoWeight;
    private final Double speechWeight;

    /** 测试/默认构造：等权，无需 Spring 容器。 */
    public FusionService() {
        this(WeightSource.EQUAL, null, null, null, null);
    }

    @Autowired
    public FusionService(
            @Value("${mact.fusion.weight-source:equal}") String weightSource,
            @Value("${mact.fusion.prior.video:0.6}") Double priorVideoWeight,
            @Value("${mact.fusion.prior.speech:0.4}") Double priorSpeechWeight,
            @Value("${mact.fusion.weights.video:}") Double videoWeight,
            @Value("${mact.fusion.weights.speech:}") Double speechWeight) {
        this(WeightSource.parse(weightSource), priorVideoWeight, priorSpeechWeight, videoWeight, speechWeight);
    }

    FusionService(WeightSource weightSource, Double priorVideoWeight, Double priorSpeechWeight,
                  Double videoWeight, Double speechWeight) {
        this.weightSource = weightSource != null ? weightSource : WeightSource.EQUAL;
        this.priorVideoWeight = priorVideoWeight;
        this.priorSpeechWeight = priorSpeechWeight;
        this.videoWeight = videoWeight;
        this.speechWeight = speechWeight;
    }

    /**
     * 决策级加权融合。
     *
     * @param modalityScores 各模态得分（0-100）；键缺失或值为 null 视为该模态无数据
     * @return 融合结果（含各模态得分/权重/贡献、总分与权重来源）；两模态皆缺失返回 null
     */
    public FusedScore lateFuse(Map<String, Double> modalityScores) {
        ResolvedWeights resolved = resolvedWeights();
        Map<String, Double> scores = new LinkedHashMap<>();
        Map<String, Double> weights = new LinkedHashMap<>();
        Map<String, Double> contributions = new LinkedHashMap<>();
        boolean hasValid = false;
        for (String modality : new String[]{VIDEO, SPEECH}) {
            double weight = VIDEO.equals(modality) ? resolved.video() : resolved.speech();
            Double raw = modalityScores == null ? null : modalityScores.get(modality);
            boolean valid = raw != null && Double.isFinite(raw);
            double score = valid ? clampScore(raw) : NEUTRAL_SCORE;
            if (valid) hasValid = true;
            scores.put(modality, score);
            weights.put(modality, weight);
            contributions.put(modality, weight * score);
        }
        if (!hasValid) return null;
        double total = contributions.values().stream().mapToDouble(Double::doubleValue).sum();
        return new FusedScore(scores, weights, contributions, clampScore(total), resolved.source());
    }

    /** 按配置解析权重来源与归一化权重；非法配置显式回退到等权。 */
    private ResolvedWeights resolvedWeights() {
        return switch (weightSource) {
            case PRIOR -> priorWeights();
            case CONFIG -> configWeights();
            case LEARNED -> learnedWeights();
            case EQUAL -> new ResolvedWeights(WeightSource.EQUAL, 0.5, 0.5);
        };
    }

    private ResolvedWeights priorWeights() {
        if (!isUsable(priorVideoWeight) || !isUsable(priorSpeechWeight)) {
            return new ResolvedWeights(WeightSource.PRIOR,
                    DEFAULT_PRIOR_VIDEO_WEIGHT, DEFAULT_PRIOR_SPEECH_WEIGHT);
        }
        return normalize(priorVideoWeight, priorSpeechWeight, WeightSource.PRIOR);
    }

    private ResolvedWeights configWeights() {
        if (!isUsable(videoWeight) || !isUsable(speechWeight)) {
            log.warn("CONFIG weight source requires both video and speech weights; "
                    + "falling back to EQUAL and reporting source=EQUAL");
            return new ResolvedWeights(WeightSource.EQUAL, 0.5, 0.5);
        }
        return normalize(videoWeight, speechWeight, WeightSource.CONFIG);
    }

    /**
     * 离线实验学习得到的权重：与 CONFIG 使用同一组配置键，但保留 LEARNED 来源标识，
     * 便于报告/复核区分“人工配置”与“数据学习”。
     */
    private ResolvedWeights learnedWeights() {
        if (!isUsable(videoWeight) && !isUsable(speechWeight)) {
            log.warn("LEARNED weight source requested but no learned weights configured; "
                    + "falling back to EQUAL and reporting source=EQUAL");
            return new ResolvedWeights(WeightSource.EQUAL, 0.5, 0.5);
        }
        return normalize(videoWeight, speechWeight, WeightSource.LEARNED);
    }

    private static ResolvedWeights normalize(Double video, Double speech, WeightSource source) {
        double v = usableWeight(video);
        double s = usableWeight(speech);
        double sum = v + s;
        if (sum <= 0) {
            return new ResolvedWeights(WeightSource.EQUAL, 0.5, 0.5);
        }
        return new ResolvedWeights(source, v / sum, s / sum);
    }

    /** 仅接受正有限权重；未配置/非正/非有限一律视为 0。 */
    private static double usableWeight(Double configured) {
        return isUsable(configured) ? configured : 0.0;
    }

    private static boolean isUsable(Double configured) {
        return configured != null && configured > 0 && Double.isFinite(configured);
    }

    private static double clampScore(double score) {
        if (!Double.isFinite(score)) return 0.0;
        return Math.max(0, Math.min(100, score));
    }

    /** 权重来源枚举（权重学习在离线实验中完成，运行时只消费）。 */
    public enum WeightSource {
        EQUAL,
        PRIOR,
        CONFIG,
        LEARNED;

        static WeightSource parse(String raw) {
            if (raw == null || raw.isBlank()) return EQUAL;
            try {
                return WeightSource.valueOf(raw.trim().toUpperCase(Locale.ROOT));
            } catch (IllegalArgumentException e) {
                log.warn("Unknown fusion weight source '{}'; falling back to EQUAL", raw);
                return EQUAL;
            }
        }
    }

    private record ResolvedWeights(WeightSource source, double video, double speech) {
    }

    public record FusedScore(
            Map<String, Double> scores,
            Map<String, Double> weights,
            Map<String, Double> contributions,
            double total,
            WeightSource weightSource) {
    }
}
