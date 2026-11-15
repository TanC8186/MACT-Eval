package com.macteval.migration;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * R02 静态契约：V31 修复 V29 补建的坏 outbox payload。
 *
 * <p><b>局限说明</b>：本测试只读迁移文件文本，断言其结构（payload 契约、精确的
 * last_error 条件、逐行异常保护、不修改 V29）。它<b>不能</b>替代在真实 PostgreSQL
 * 上执行 Flyway 迁移的验证：SQL 语法、JSON 解析失败时的 EXCEPTION 行为、
 * CHECK/唯一约束冲突等只有真正跑库才能证实。</p>
 */
class OutboxPayloadRepairMigrationTest {

    private static final Path MIGRATIONS = Path.of("src/main/resources/db/migration");

    private static String read(String fileName) throws IOException {
        Path path = MIGRATIONS.resolve(fileName);
        assertTrue(Files.exists(path), "迁移文件不存在: " + path);
        return Files.readString(path, StandardCharsets.UTF_8);
    }

    @Test
    void v31RebuildsPayloadWithTheQueueContract() throws IOException {
        String sql = read("V31__repair_v29_outbox_payloads.sql");

        assertTrue(sql.contains("jsonb_build_object"), "必须按入队契约重新构造 JSON 对象");
        assertTrue(sql.contains("'id'"), "payload 必须含任务 id");
        assertTrue(sql.contains("'type'"), "payload 必须含任务 type");
        assertTrue(sql.contains("'priority'"), "payload 必须含任务 priority");
        // 展平 params：与 AnalysisJobService.createJob 的 message.putAll(parsedParams) 等价。
        assertTrue(sql.matches("(?s).*\\|\\|\\s*COALESCE\\(.*params.*\\).*"),
                "必须把 params 展平合并进 payload，而不是整串塞入");
        // 重新投递所需的重置。
        assertTrue(sql.contains("published_at = NULL"));
        assertTrue(sql.contains("attempt = 0"));
        assertTrue(sql.contains("next_retry_at"));
    }

    @Test
    void v31OnlyTouchesTheV29BackfillRows() throws IOException {
        String sql = read("V31__repair_v29_outbox_payloads.sql");

        assertTrue(sql.contains("o.last_error = 'inserted by V29 backfill'"),
                "必须用 V29 写入的精确 last_error 界定范围");
        // 不得批量重置所有 QUEUED 任务。
        assertFalse(sql.matches("(?i)(?s).*update\\s+analysis_jobs.*") ,
                "V31 只应修 outbox 事件，不得改写 analysis_jobs");
    }

    @Test
    void v31GuardsEachRowAgainstInvalidJson() throws IOException {
        String sql = read("V31__repair_v29_outbox_payloads.sql");

        assertTrue(sql.contains("DO $$"), "必须用 DO 块实现逐行保护");
        assertTrue(sql.contains("EXCEPTION WHEN others"), "单行非法 JSON 不得让整条迁移失败");
        assertTrue(sql.contains("RAISE NOTICE"), "跳过的行必须留下 NOTICE 记录");
    }

    @Test
    void v29IsNotModifiedByTheRepair() throws IOException {
        String v29 = read("V29__converge_stuck_running_jobs.sql");

        // V29 已执行过，Flyway 校验和必须不变：它依旧写 j.params 作为 payload。
        assertTrue(v29.contains("SELECT md5(j.id || ':V29'), j.id, 'ANALYSIS_JOB_QUEUED', j.params, 0,"),
                "V29 内容不得被修改（否则 Flyway 校验和失效）");
        assertFalse(v29.contains("jsonb_build_object"),
                "V29 不应被回填契约改写；修复必须在新的编号迁移里完成");
    }

    @Test
    void migrationNumbersAreSequentialAndUnique() throws IOException {
        List<Path> files;
        try (var stream = Files.list(MIGRATIONS)) {
            files = stream.filter(p -> p.getFileName().toString().startsWith("V"))
                    .toList();
        }
        assertTrue(files.stream().anyMatch(p -> p.getFileName().toString()
                        .startsWith("V30__")),
                "R04 的窗口版本化列必须是 V30");
        assertTrue(files.stream().anyMatch(p -> p.getFileName().toString()
                        .startsWith("V31__")),
                "R02 的 outbox 修复必须是 V31");
        long distinct = files.stream()
                .map(p -> p.getFileName().toString().split("__")[0])
                .distinct().count();
        assertTrue(distinct == files.size(), "迁移编号不得重复: " + files.size() + " files, "
                + distinct + " distinct numbers");
    }

    // ================================================================
    // V33：收紧 V31 的 payload 修复边界（非对象 params 不得生成可发送消息）
    // ================================================================

    /** V33 是 V32 之后的下一个编号，且整个 V 序列连续、无重复。 */
    @Test
    void v33IsTheNextSequentialMigrationAfterV32() throws IOException {
        List<Path> files;
        try (var stream = Files.list(MIGRATIONS)) {
            files = stream.filter(p -> p.getFileName().toString().matches("V\\d+__.*\\.sql"))
                    .toList();
        }
        List<Integer> numbers = files.stream()
                .map(p -> Integer.parseInt(p.getFileName().toString().split("__")[0].substring(1)))
                .sorted()
                .toList();
        assertTrue(numbers.stream().anyMatch(n -> n == 33),
                "必须存在 V33 迁移文件: " + files);
        assertTrue(numbers.get(numbers.size() - 1) == 33,
                "V33 必须是当前最大编号（V32 已被占用），实际最大 = "
                        + numbers.get(numbers.size() - 1));
        for (int i = 1; i < numbers.size(); i++) {
            assertTrue(numbers.get(i) == numbers.get(i - 1) + 1,
                    "迁移编号必须连续，缺口位于 " + numbers.get(i - 1) + " -> " + numbers.get(i));
        }
        Path v33 = files.stream()
                .filter(p -> p.getFileName().toString().startsWith("V33__"))
                .findFirst().orElseThrow();
        assertTrue(v33.getFileName().toString().endsWith(".sql"), "V33 必须是 .sql 文件");
        assertFalse(v33.getFileName().toString().contains("repair_v29_outbox_payloads"),
                "V33 不得覆盖/替代 V31 的文件名（V31 已执行，只能新增）");
    }

    /** V33 不得改写已执行的 V29/V31 文本，避免 Flyway 校验和失配。 */
    @Test
    void v33LeavesV29AndV31TextUntouched() throws IOException {
        String v29 = read("V29__converge_stuck_running_jobs.sql");
        String v31 = read("V31__repair_v29_outbox_payloads.sql");

        // 与已有断言同源的锚点：两处原文必须仍是历史形态。
        assertTrue(v29.contains("SELECT md5(j.id || ':V29'), j.id, 'ANALYSIS_JOB_QUEUED', j.params, 0,"),
                "V29 的补建语句原文不得改动");
        assertTrue(v31.contains("COALESCE(NULLIF(btrim(r.params), '')::jsonb, '{}'::jsonb)"),
                "V31 的合并语句原文不得改动（缺陷保留在历史迁移里，由 V33 收拾）");
        assertTrue(v31.contains("'repaired by V31'"),
                "V31 写入的 last_error 标记必须仍是 'repaired by V31'（V33 依赖它界定范围）");
        assertTrue(v31.contains("'inserted by V29 backfill'"),
                "V29 写入的 last_error 标记必须仍是 'inserted by V29 backfill'");
    }

    /** 范围必须精确收在两个 last_error 上，且不得改 analysis_jobs。 */
    @Test
    void v33ScopeIsOnlyTheTwoHistoricalMarkers() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        assertTrue(sql.contains("'inserted by V29 backfill'"), "必须覆盖 V29 插入的行");
        assertTrue(sql.contains("'repaired by V31'"), "必须覆盖 V31 尝试修复过的行");
        assertTrue(sql.matches("(?s).*IN\\s*\\(\\s*'inserted by V29 backfill'\\s*,\\s*'repaired by V31'\\s*\\).*"),
                "两个标记必须出现在同一个 IN 列表里，不要多加别的 last_error");

        // 不得变成"所有 outbox 行都修"，也不得触碰任务表。
        assertFalse(sql.matches("(?i)(?s).*from\\s+task_outbox\\s+(o\\s+)?where\\s+true.*"),
                "不得无条件全表扫描式修复");
        assertFalse(sql.matches("(?i)(?s).*update\\s+analysis_jobs.*"),
                "V33 只修 outbox 事件，不得改写 analysis_jobs");
        assertFalse(sql.matches("(?i)(?s).*delete\\s+from\\s+analysis_jobs.*"),
                "V33 不得删除任务行");
        assertFalse(sql.matches("(?i)(?s).*delete\\s+from\\s+task_outbox.*"),
                "V33 不得删除 outbox 事件（parked 行必须留痕）");
    }

    /** 关键缺陷修复点：params 和**既有 payload** 都要 jsonb_typeof 判型。 */
    @Test
    void v33ChecksJsonbTypeOfBothParamsAndExistingPayload() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        assertTrue(sql.contains("jsonb_typeof"), "必须显式判型，不能靠 CAST 成功与否");
        assertTrue(sql.matches("(?s).*jsonb_typeof\\(\\s*v_params\\s*\\).*")
                        || sql.contains("v_param_type := jsonb_typeof(v_params)"),
                "必须对 params 判型");
        assertTrue(sql.matches("(?s).*jsonb_typeof\\(\\s*v_payload_trim\\s*::jsonb\\s*\\).*"),
                "必须对**既有 payload** 判型（V31 可能已写出非对象 jsonb，这是真实生产隐患）");
        // 既有 payload 非对象时，本迁移的处理顺序是：
        //   1) 若 params 仍是合法对象 -> 用 params 重建出对象 payload（修复）；
        //   2) 若 params 也不是对象   -> park。
        // 因此断言应落在"类型参与判定"，而不是"必然进入重建"。
        assertTrue(sql.contains("v_payload_type IS DISTINCT FROM 'object'"),
                "必须用既有 payload 的类型参与判定（非对象时不得当作已修好）");

        // btrim(x) 只去空格，不去 tab/换行；空白判定必须显式给出字符集，
        // 否则 params = "   \t" 会被误判成"非法 JSON"（真机验证时就是这样暴露的）。
        //
        // 【不得使用 E'...\v'】PostgreSQL 的 E'' 字符串没有 \v 转义：那 6 个字符
        // 是 空格/TAB/CR/LF/FF/字母 'v'。用它会 btrim 掉真正的字母 v，把非法 JSON
        // '{"a":1}v' 洗成合法 JSON '{"a":1}' 并当作"可发送"发出去。真机验证过。
        assertFalse(sql.matches("(?s).*E'[^']*\\v[^']*'.*"),
                "空白字符集不得用 E'...\\v'（在 PostgreSQL 里那是字母 v，不是 VT）");
        assertTrue(sql.contains("chr(11)"),
                "垂直制表符必须用 chr(11) 拼出，不能依赖 E'\\v'");
        assertTrue(sql.contains("btrim(r.payload, v_ws)"),
                "payload 的空白判定必须包含 tab/换行/VT，不能只用 btrim(x)");
        assertTrue(sql.contains("btrim(r.params, v_ws)"),
                "params 的空白判定必须包含 tab/换行/VT，不能只用 btrim(x)");

        // 四类非对象 params 必须被点名（array/string/number/boolean 走 park）。
        for (String kind : List.of("'object'", "'empty'", "'invalid'")) {
            assertTrue(sql.contains(kind), "分类分支必须包含 " + kind);
        }
        assertTrue(sql.matches("(?s).*v_param_type\\s+IN\\s*\\(\\s*'object'\\s*,\\s*'empty'\\s*\\).*"),
                "只有 object/empty 才允许走可发送路径；其余类型一律 park（这是 V31 的漏洞）");

        // Jackson 的两条 StreamReadConstraints 都必须被显式建模，否则"合法 JSON 对象"
        // 仍可能在发布时无条件抛错并永久重试（真机复现过两种）：
        //   maxNestingDepth = 1000 -> 嵌套过深
        //   maxNumberLength = 1000 -> 标量超长数字（深度为 0，深度守卫拦不住）
        assertTrue(sql.contains("v33_json_depth"), "必须校验嵌套深度（Jackson maxNestingDepth）");
        assertTrue(sql.contains("v33_max_number_len"),
                "必须校验数值字面量长度（Jackson maxNumberLength，与深度无关）");
        assertTrue(sql.contains("IF v_depth > 200 THEN"), "深度阈值必须显式且远低于 1000");
        assertTrue(sql.contains("IF v_num_len > 700 THEN"),
                "数值长度阈值必须显式且低于 1000");
        // Jackson 的第三条上限 maxStringLength = 20000000，同样与深度无关。
        assertTrue(sql.contains("v33_max_string_len"),
                "必须校验字符串长度（Jackson maxStringLength）");
        assertTrue(sql.contains("IF v_str_len > 1000000 THEN"),
                "字符串长度阈值必须显式且远低于 20000000");
        assertTrue(sql.contains("DROP FUNCTION IF EXISTS v33_max_string_len"),
                "字符串长度辅助函数同样必须在迁移末尾删除");
        // Jackson 的第四条上限 maxNameLength = 50000，只约束对象**键**：
        // 字符串守卫只看值，键超长不会被它拦下。
        assertTrue(sql.contains("v33_max_name_len"),
                "必须校验对象键长度（Jackson maxNameLength）");
        assertTrue(sql.contains("IF v_name_len > 40000 THEN"),
                "键长度阈值必须显式且低于 50000");
        assertTrue(sql.contains("DROP FUNCTION IF EXISTS v33_max_name_len"),
                "键长度辅助函数同样必须在迁移末尾删除");
        // 四条约束必须在同一张契约表里列全，避免再漏下一条。
        for (String bound : List.of("maxNestingDepth", "maxNumberLength",
                                    "maxStringLength", "maxNameLength")) {
            assertTrue(sql.contains(bound), "头部契约表必须列出 " + bound);
        }
        assertTrue(sql.contains("DROP FUNCTION IF EXISTS v33_json_depth"),
                "辅助函数必须在迁移末尾删除，不留在业务 schema");
        assertTrue(sql.contains("DROP FUNCTION IF EXISTS v33_max_number_len"),
                "数值长度辅助函数同样必须删除");

        // 审计写不进去时必须 fail closed：否则该行保持可发送且没有审计记录，
        // 正是本迁移要消灭的"静默失修"。
        assertTrue(sql.contains("RAISE EXCEPTION 'V33: cannot record repair outcome"),
                "审计不可写时必须 RAISE EXCEPTION 让迁移失败，而不是只 WARNING");

        // task_outbox 没有指向 analysis_jobs 的外键：孤儿行不能被 INNER JOIN 静默吞掉。
        assertTrue(sql.contains("LEFT JOIN analysis_jobs j"), "必须用 LEFT JOIN，否则孤儿事件被静默跳过");
        assertTrue(sql.contains("'orphan'"), "孤儿事件必须单独分类并 park");
        assertTrue(sql.contains("repair_failed: analysis_jobs row missing"),
                "孤儿事件必须有可辨识的 last_error");
    }

    /** 审计表是持久记录，取代 V31 不可查询的 RAISE NOTICE。 */
    @Test
    void v33WritesDurableAuditRows() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        assertTrue(sql.matches("(?s).*CREATE TABLE IF NOT EXISTS outbox_repair_audit.*"),
                "必须建持久审计表");
        assertTrue(sql.contains("BIGSERIAL PRIMARY KEY"), "审计表用 bigserial 主键");
        for (String column : List.of("outbox_id", "aggregate_id", "outcome", "detail", "created_at")) {
            assertTrue(sql.contains(column), "审计表必须含列 " + column);
        }
        for (String outcome : List.of("'repaired'", "'parked'", "'skipped_not_json'")) {
            assertTrue(sql.contains(outcome), "outcome 必须覆盖 " + outcome);
        }
        assertTrue(sql.contains("COMMENT ON TABLE outbox_repair_audit IS"),
                "审计表必须有 COMMENT ON TABLE");
        assertTrue(sql.contains("COMMENT ON COLUMN task_outbox.payload IS"),
                "payload 新契约必须有 COMMENT");
        assertTrue(sql.matches("(?s).*CREATE UNIQUE INDEX IF NOT EXISTS \\w+\\s*\\n?\\s*ON outbox_repair_audit.*"),
                "(outbox_id, outcome) 需要唯一索引支撑幂等");
    }

    /** 非对象 params 必须 park 而不是发送；parked 行绝不能把 published_at 置 NULL。 */
    @Test
    void v33ParksUnsendableRowsInsteadOfSendingThem() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        assertTrue(sql.contains("repair_failed: params is "), "parked 行必须有可辨识的 last_error");
        assertTrue(sql.contains("repair_failed: params is not valid json"),
                "非法 JSON 必须有独立标记");
        assertTrue(sql.contains("INTERVAL '100 years'"),
                "parked 行必须把 next_retry_at 推到极远未来，publisher 永远选不中");

        // park 必须同时对**两条**运行期重发送查询都不可见（真机验证过）：
        //   findPending                     -> published_at IS NULL AND next_retry_at <= now
        //   findStalePublishedForQueuedJobs -> published_at IS NOT NULL（完全不看 next_retry_at）
        // 所以稳定的 parked 状态是 「published_at = NULL 且 next_retry_at 推远」：
        // 只推 next_retry_at 会留一个缺口——已发布过的坏行会被 reconciler 在 ~300s 后
        // 重新武装（published_at 清零、attempt 归零），无限退避循环原样复活。
        String parkBlock = between(sql, "[V33-PARK]", "[/V33-PARK]");
        assertFalse(parkBlock.isBlank(), "必须保留 [V33-PARK] 标记块以便本测试校验");
        assertTrue(parkBlock.contains("published_at  = NULL")
                        || parkBlock.contains("published_at = NULL"),
                "park 分支必须把 published_at 置 NULL，否则 reconciler 会重新武装它");
        assertTrue(parkBlock.contains("next_retry_at"), "park 分支必须推 next_retry_at");
        assertTrue(parkBlock.contains("INTERVAL '100 years'"),
                "park 分支必须把 next_retry_at 推到极远未来，findPending 永远选不中");

        // 唯一允许 published_at = NULL 的是修复分支。
        String repairBlock = between(sql, "[V33-REPAIR]", "[/V33-REPAIR]");
        assertFalse(repairBlock.isBlank(), "必须保留 [V33-REPAIR] 标记块");
        assertTrue(repairBlock.contains("published_at  = NULL")
                        || repairBlock.contains("published_at = NULL"),
                "修复分支必须重新置 published_at=NULL 以便重新投递");
        // 两处合法的 published_at=NULL：
        //   修复分支——必须重新打开投递；
        //   park 分支——必须让 reconciler（只看 published_at）也选不中。
        // 除此之外任何地方都不该出现，否则会凭空制造可发送状态。
        // 每一处 `published_at = NULL` 都是一次"重新武装"，必须成对出现
        // next_retry_at = far future（不可发送路径）或 next_retry_at = NOW()（修复路径）。
        // 用配对计数锁定：赋值次数 == 紧邻的 next_retry_at 赋值次数。
        String[] lines = sql.split("\\R");
        int publishedAtNullCount = 0;
        int pairedWithRetry = 0;
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            if (!line.trim().startsWith("published_at")) continue;
            if (!line.contains("NULL")) continue;
            publishedAtNullCount++;
            for (int j = Math.max(0, i - 3); j < Math.min(lines.length, i + 4); j++) {
                if (lines[j].trim().startsWith("next_retry_at")) {
                    pairedWithRetry++;
                    break;
                }
            }
        }
        assertTrue(publishedAtNullCount >= 2,
                "至少要有修复分支与不可发送分支两处 published_at=NULL，实际 " + publishedAtNullCount);
        assertTrue(pairedWithRetry == publishedAtNullCount,
                "每处 published_at=NULL 都必须与 next_retry_at 赋值配对，实际配对 "
                        + pairedWithRetry + "/" + publishedAtNullCount);
    }

    /** 幂等 + 逐行异常保护。 */
    @Test
    void v33IsIdempotentAndGuardsEachRow() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        assertTrue(sql.contains("DO $$"), "必须用 DO 块实现逐行保护");
        assertTrue(sql.contains("EXCEPTION WHEN others"), "单行异常不得让整条迁移失败");
        assertTrue(sql.contains("'repaired by V33'"), "修复后必须写新标记，供二次执行界定范围");
        // NULL 不安全比较会让 last_error IS NULL 的历史行整条漏掉 —— 而
        // publishPending 投递成功时恰好把 last_error 清成 NULL（真机复现过）。
        assertTrue(sql.contains("o.last_error IS DISTINCT FROM 'repaired by V33'"),
                "必须用 IS DISTINCT FROM 排除已修复行；用 <> 会漏掉 last_error IS NULL 的行");
        assertFalse(sql.matches("(?s).*o\\.last_error\\s*<>\\s*'repaired by V33'.*"),
                "不得使用 NULL 不安全的 <> 比较");
        assertTrue(sql.contains("NOT EXISTS"), "审计写入前必须 NOT EXISTS 判重");
        assertTrue(sql.matches("(?s).*FOR UPDATE OF o.*"),
                "必须对 outbox 行加锁，保证并发执行安全");
        assertTrue(sql.contains("GET DIAGNOSTICS"), "必须检查 UPDATE 实际影响行数");
        assertTrue(sql.contains("RAISE WARNING"),
                "行级失败必须留下 WARNING（并配合审计行，而不是只 NOTICE）");

        // 只排除 'repaired by V33' 不够：parked 行写的是 'repair_failed: ...'，
        // 若不排除，每次执行都会刷新它们的 next_retry_at（真机验证时快照比较失败）。
        assertTrue(sql.matches("(?s).*NOT LIKE 'repair\\\\_failed:%.*"),
                "必须同时排除 repair_failed: 前缀，否则 parked 行每次执行都被重写");
    }

    /**
     * last_error 会被 publisher 覆写，所以范围不能只靠标记；
     * 必须同时认 V29 留下的 md5 指纹，且不得把已修好的行重新打开投递。
     */
    @Test
    void v33CatchesDriftedRowsByV29FingerprintWithoutResendingRepairedOnes() throws IOException {
        String sql = read("V33__harden_v29_outbox_payload_repair.sql");

        // 1) 范围必须包含 V29 指纹分支，否则 last_error 被 publisher 覆写后
        //    那批最坏的历史行会永久掉出范围、无限退避重试。
        assertTrue(sql.contains("md5(o.aggregate_id || ':V29')"),
                "必须用 V29 的 id 指纹兜住 last_error 已被覆写的历史行");
        assertTrue(sql.contains("by_fingerprint"), "指纹命中的行必须单独标记，便于只对它加严格条件");
        assertTrue(sql.contains("by_marker"), "标记命中的行必须单独标记，便于区分两类来源");

        // 2) 指纹分支必须同时要求 event_type，避免撞上同 id 的其它事件类型。
        assertTrue(sql.matches("(?s).*o\\.event_type = 'ANALYSIS_JOB_QUEUED'\\s*\\n?\\s*AND o\\.id = md5.*"),
                "指纹必须同时约束 event_type = ANALYSIS_JOB_QUEUED");

        // 3) 已经带完整信封的指纹行（V31 修好且正常发布过）不得被重新打开投递，
        //    否则会重复发送。判定必须看 payload 里是否真有 id/type/priority。
        String guard = sql.contains("full envelope") ? sql.substring(0, sql.indexOf("full envelope")) : "";
        assertFalse(guard.isBlank(), "必须有'已是完整信封就跳过'的守卫，并留下可检索的文字说明");
        // by_marker 是 o.last_error IN (...) —— last_error 为 NULL 时它求值为 NULL，
        // `AND NOT NULL` 仍是 NULL，于是守卫会把该行整条排除。而 last_error 被
        // publisher 清成 NULL 恰恰是这个分支最常见的形态（真机复现过）。
        assertTrue(sql.matches("(?s).*IF r\\.by_fingerprint\\s*\\n?\\s*AND r\\.by_marker IS NOT TRUE.*CONTINUE;.*"),
                "守卫必须用 `r.by_marker IS NOT TRUE`（NULL 安全），不能用 `NOT r.by_marker`");
        assertFalse(sql.matches("(?s).*AND NOT r\\.by_marker.*"),
                "不得使用 NULL 不安全的 `NOT r.by_marker`");
        // 信封判定必须取**值**：{"id":null} 的 ? 'id' 为真，但 worker 会拿到空 id。
        for (String key : List.of("id", "type", "priority")) {
            assertTrue(sql.contains("->> '" + key + "'"),
                    "信封完整性判定必须用 ->> 取 " + key + " 的值，不能用 ? 只判键存在");
        }
        assertFalse(sql.contains("? 'id'"),
                "不能用 ? 'id' 判存在性 —— {\"id\":null} 会被误判为完整信封");
    }

    /** 取两个标记之间的文本；找不到返回空串。 */
    private static String between(String text, String startMarker, String endMarker) {
        int start = text.indexOf(startMarker);
        if (start < 0) {
            return "";
        }
        int end = text.indexOf(endMarker, start + startMarker.length());
        if (end < 0) {
            return "";
        }
        return text.substring(start + startMarker.length(), end);
    }
}
