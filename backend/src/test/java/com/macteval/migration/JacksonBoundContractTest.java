package com.macteval.migration;

import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * V33 的四条 Jackson 上限阈值必须与**运行时实际生效的值**保持安全余量。
 *
 * <p>为什么需要这个测试：V33 的阈值是从 {@code StreamReadConstraints.defaults()}
 * 抄下来的常量（200 / 700 / 1000000 / 40000）。而 {@code jackson-bom.version} 是从
 * Spring Boot 父 POM 继承的，一次 3.3 → 3.4 的升级就可能改变其中任何一个数字，
 * 而 V33 一行都不用改、静态测试也照样全绿——守卫会静默失效。</p>
 *
 * <p>本测试不用硬编码的期望值，而是读当前 classpath 上 Jackson 的真实默认值，
 * 断言 V33 里写死的阈值仍然严格小于它们。升级 Jackson 若把上限调低到阈值以下，
 * 这里会直接失败，而不是等到生产环境出现"永久重试"的消息。</p>
 */
class JacksonBoundContractTest {

    private static final Path V33 = Path.of(
            "src/main/resources/db/migration/V33__harden_v29_outbox_payload_repair.sql");

    /** V33 里守卫与阈值的一一对应关系：SQL 片段 → 当前 Jackson 上限的 getter 名。 */
    private record Guard(String sqlFragment, String boundName, int v33Threshold) {
    }

    private static final List<Guard> GUARDS = List.of(
            new Guard("IF v_depth > 200 THEN", "maxNestingDepth", 200),
            new Guard("IF v_num_len > 700 THEN", "maxNumberLength", 700),
            new Guard("IF v_str_len > 1000000 THEN", "maxStringLength", 1_000_000),
            new Guard("IF v_name_len > 40000 THEN", "maxNameLength", 40_000));

    @Test
    void everyGuardThresholdStaysBelowTheRuntimeJacksonLimit() throws IOException {
        StreamReadConstraints c = StreamReadConstraints.defaults();
        int depth = c.getMaxNestingDepth();
        int number = c.getMaxNumberLength();
        int string = c.getMaxStringLength();
        int name = c.getMaxNameLength();

        // 先把"真实值"打印出来：升级 Jackson 后看测试日志就知道新数字是多少。
        System.out.printf("StreamReadConstraints defaults: depth=%d number=%d string=%d name=%d%n",
                depth, number, string, name);

        // 【必须从 Guard.v33Threshold() 取阈值，不能在这里再写一遍字面量】
        // 阈值若同时存在于 Guard 和本方法，两个人各改一处就会静默漂移；
        // 让 record 成为唯一来源，加第五条上限时只需追加一行 Guard。
        for (Guard g : GUARDS) {
            int live = liveBound(c, g.boundName());
            assertTrue(g.v33Threshold() < live,
                    "V33 的 " + g.boundName() + " 阈值 " + g.v33Threshold()
                            + " 必须严格低于 Jackson 运行时上限 " + live);
        }

        // 与受版本控制的校准基准比较（src/test/resources/jackson-bounds.json）。
        // 上面的"阈值 < 运行时"只能保证今天安全；这个比对负责回答
        // "Jackson 是不是在背后变了"——运行时上限一旦低于记录值，余量就被吃掉了，
        // 必须重新校准 V33，而不是继续用旧阈值。
        JsonNode recorded = readRecordedBounds();
        for (Guard g : GUARDS) {
            int live = liveBound(c, g.boundName());
            int baseline = recorded.get("streamReadConstraints").get(g.boundName()).asInt();
            assertTrue(live >= baseline,
                    "Jackson " + g.boundName() + " 从记录的 " + baseline
                            + " 降到了运行时 " + live + "：V33 的阈值余量被吃掉，"
                            + "需要重新校准阈值并更新 jackson-bounds.json");
            assertTrue(recorded.get("v33Thresholds").get(thresholdKey(g.boundName())).asInt()
                            == g.v33Threshold(),
                    "jackson-bounds.json 里记录的 V33 阈值与测试常量不一致："
                            + g.boundName());
        }

        // maxDocumentLength 目前是 -1（不限制），所以 V33 没有对应守卫。
        // 这里不是"验证它安全"，而是**哨兵**：一旦某次 Jackson/Boot 升级把它变成
        // 有限值，这条会失败，提示"出现了第五条 V33 未建模的永久重试成因"。
        long docLen = c.getMaxDocumentLength();
        assertTrue(docLen < 0,
                "Jackson maxDocumentLength 从 -1（不限制）变成了 " + docLen
                        + "：这是 V33 未建模的第五条上限，需要在迁移里加守卫并更新"
                        + " jackson-bounds.json 与本测试");
    }

    /** bound 名 → 运行时上限值（唯一的 switch，加新上限时只改这里）。 */
    private static int liveBound(StreamReadConstraints c, String boundName) {
        return switch (boundName) {
            case "maxNestingDepth" -> c.getMaxNestingDepth();
            case "maxNumberLength" -> c.getMaxNumberLength();
            case "maxStringLength" -> c.getMaxStringLength();
            case "maxNameLength" -> c.getMaxNameLength();
            default -> throw new IllegalStateException("unknown bound " + boundName);
        };
    }

    /** bound 名 → jackson-bounds.json 里 V33 阈值条目的键名。 */
    private static String thresholdKey(String boundName) {
        return switch (boundName) {
            case "maxNestingDepth" -> "depth";
            case "maxNumberLength" -> "numberLength";
            case "maxStringLength" -> "stringLength";
            case "maxNameLength" -> "nameLength";
            default -> throw new IllegalStateException("unknown bound " + boundName);
        };
    }

    /** 读 src/test/resources 下的校准基准（随源码受版本控制）。 */
    private static JsonNode readRecordedBounds() throws IOException {
        try (var in = JacksonBoundContractTest.class.getResourceAsStream("/jackson-bounds.json")) {
            assertNotNull(in, "缺少 src/test/resources/jackson-bounds.json（阈值的校准基准）");
            return new ObjectMapper().readTree(in);
        }
    }

    /**
     * 阈值必须真的写在 SQL 里、且与上面的表一致。
     *
     * <p>只断言"小于运行时上限"是不够的：把阈值从 40000 改成 1 也能通过。
     * 这里锁定 SQL 中出现的字面量，保证清单与实现不会各自漂移。</p>
     */
    @Test
    void guardThresholdsArePresentInTheMigration() throws IOException {
        String sql = Files.readString(V33, StandardCharsets.UTF_8);
        for (Guard guard : GUARDS) {
            assertTrue(sql.contains(guard.sqlFragment()),
                    "V33 缺少守卫片段: " + guard.sqlFragment());
        }
    }

    /** 四个辅助函数必须都在迁移末尾被删除，不留在业务 schema。 */
    @Test
    void allHelperFunctionsAreDroppedAtTheEnd() throws IOException {
        String sql = Files.readString(V33, StandardCharsets.UTF_8);
        for (String fn : List.of("v33_json_depth", "v33_max_number_len",
                "v33_max_string_len", "v33_max_name_len")) {
            assertTrue(sql.contains("DROP FUNCTION IF EXISTS " + fn),
                    "缺少 DROP FUNCTION IF EXISTS " + fn);
        }
    }

    /**
     * 契约表必须把四条上限都列出来。
     *
     * <p>这张表是"下次别再漏一条"的唯一防线：前三轮评审各发现一条遗漏的上限，
     * 唯一可扩展的检查就是断言表里出现的 bound 名数量与守卫数量一致。</p>
     */
    @Test
    void contractTableListsEveryBound() throws IOException {
        String sql = Files.readString(V33, StandardCharsets.UTF_8);
        List<String> missing = new ArrayList<>();
        for (Guard guard : GUARDS) {
            if (!sql.contains(guard.boundName())) {
                missing.add(guard.boundName());
            }
        }
        assertTrue(missing.isEmpty(), "契约表缺少上限: " + missing);
    }

    /**
     * 走一遍真实反序列化：把 Jackson 真正会拒绝的输入送进 readValue(Map)。
     *
     * <p>前三个测试都是"数字对得上"，但数字对不等于行为对——V33 的全部前提是
     * "超限的 payload 会让 publishPending 无条件抛错"。这里用与
     * {@code TaskOutboxPublisher} 完全相同的调用验证这一点，否则整套守卫可能是
     * 在防一个不存在的问题。</p>
     */
    @Test
    void overLimitPayloadsAreRejectedByTheRealPublisherCall() {
        ObjectMapper mapper = new ObjectMapper();
        StreamReadConstraints c = StreamReadConstraints.defaults();

        // 1) 深度超限：嵌套比 Jackson 上限更深的对象。
        StringBuilder deep = new StringBuilder();
        for (int i = 0; i < c.getMaxNestingDepth() + 5; i++) {
            deep.append("{\"k\":");
        }
        deep.append("1");
        for (int i = 0; i < c.getMaxNestingDepth() + 5; i++) {
            deep.append('}');
        }
        assertReadFails(mapper, deep.toString(), "嵌套深度");

        // 2) 数值超限：单个整数比 maxNumberLength 更长。
        String bigNumber = "{\"n\":" + "9".repeat(c.getMaxNumberLength() + 5) + "}";
        assertReadFails(mapper, bigNumber, "数值长度");

        // 3) 字符串值超限：值比 maxStringLength 更长。
        String bigString = "{\"s\":\"" + "x".repeat(c.getMaxStringLength() + 5) + "\"}";
        assertReadFails(mapper, bigString, "字符串长度");

        // 4) 对象键超限：键比 maxNameLength 更长。
        String bigName = "{\"" + "k".repeat(c.getMaxNameLength() + 5) + "\":\"v\"}";
        assertReadFails(mapper, bigName, "对象键长度");
    }

    /** 用 publishPending 的同款调用读一次，必须抛异常而不是拿到 Map。 */
    private static void assertReadFails(ObjectMapper mapper, String payload, String what) {
        Throwable caught = null;
        try {
            mapper.readValue(payload, java.util.Map.class);
        } catch (Throwable t) {  // noqa: 故意捕获全部，只为断言"抛了"
            caught = t;
        }
        assertNotNull(caught,
                what + " 超过 Jackson 上限时必须抛异常（否则 V33 的守卫在防一个不存在的问题）");
        String message = String.valueOf(caught.getMessage()).toLowerCase();
        assertTrue(message.contains("length") || message.contains("depth")
                        || message.contains("constraints") || message.contains("maximum"),
                what + " 的异常应来自 StreamReadConstraints，实际: " + caught);
    }
}
