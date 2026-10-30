# T2 · 真实 LLM 评测与 LLM-as-judge 报告

> 生成时间：2026-10-30T04:15:05+00:00

## ⚠️ 结论：SKIPPED（未检测到真实模型密钥）

未发现可用的 provider 密钥（检测项：OPENAI_API_KEY / DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）。按「缺失则跳过并标注」原则，**未执行真实模型评测**，不产出真实通过率与分歧率，以免用假数据充数。

**当前仓库内可复现的确定性结果（P1-5 离线规则评测）**：

- `python scripts/run_eval.py` → **19/19（100%）**（risk 6/6、injection 3/3、retrieval 10/10）

**补齐真实评测所需步骤**：

```bash
# 任选其一注入密钥后执行（deepseek 走 OpenAI 兼容协议）
export OPENAI_API_KEY=sk-xxxx        # 或
export DASHSCOPE_API_KEY=sk-xxxx     # 或
export DEEPSEEK_API_KEY=sk-xxxx
python scripts/run_eval_llm.py --report docs/16-真实LLM评测报告.md
```

**为什么现在不能给真实数字**：真实通过率/分歧率必须由真实模型产生；本环境无密钥，也不应伪造。脚本已实现并用单测覆盖了「模型调用→规则判定→judge 判定→分歧统计→失败清单」全流程（见 `tests/test_eval_llm.py`），注入密钥即可产出真实数字。

**retrieval 用例（与模型无关，可离线复现）**：10/10（100%）

## 诚实标注

- 生产链路 `build_llm()` 对真实 provider 已套 `LLMGateway`（超时/重试/限流/降级），**rule 仍是 fallback**：真实模型失败时不中断工作流，但会以 `outcome=fallback` 计入指标。
- 真实模型评测应关注「不是 100%」的那些用例：那才是模型与规则的真实差异。
- judge 默认复用同一 provider（独立 call、独立 prompt）；生产建议换用更强/不同模型以降偏差。
