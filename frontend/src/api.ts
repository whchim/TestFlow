/**
 * Gateway 的 HTTP 客户端。
 *
 * 对应决策：`05-API契约.md`（接口契约）、API4（`problem+json`）、API7（REST 是权威）、
 * PRD F9（前端不重复实现业务规则）。
 *
 * ## 三件刻意不做的事
 *
 * 1. **不重排用例。** 置信度排序由服务端给（`GET /api/runs/{id}/cases` 的
 *    排序就是接口语义）。前端自己再排一遍，两个"哪个排前面"的定义迟早
 *    不一致——而那时用户看到的是"界面上的顺序和分页对不上"。
 * 2. **不解释 `phase` 字符串。** 除了"展示"与"是否终态"之外，界面对阶段
 *    不做任何推断。阶段一改名，散落在组件里的 `if (phase === 'xxx')`
 *    会静默失效。
 * 3. **不缓存状态。** 每次需要状态就查一次（API7）。SSE 只用来知道
 *    "该查了"，它不携带数据。
 */

/** Gateway 的错误体（RFC 9457 + 本项目的 `code` 扩展）。 */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail?: string
  code?: string
  /** 解析类错误的逐条定位（`05` §4.1）。 */
  errors?: { pointer: string; message: string }[]
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly problem: ProblemDetail | undefined

  constructor(status: number, problem: ProblemDetail | undefined) {
    // `title` 是给人读的，`detail` 补充原因。两者都拼上——
    // 只显示 title 会让用户看到"决策不完整"却不知道为什么。
    const text = [problem?.title, problem?.detail].filter(Boolean).join('：')
    super(text || `请求失败（HTTP ${status}）`)
    this.status = status
    this.code = problem?.code ?? `http_${status}`
    this.problem = problem
  }
}

export interface WarningItem {
  code: string
  detail: string
}

export interface SpecCreated {
  spec_id: string
  fingerprint: string
  openapi_version: string
  operation_count: number
  declared_targets: string[]
  reused: boolean
  warnings: WarningItem[]
}

export interface RunStatus {
  run_id: string
  phase: string
  spec_fingerprint: string
  expected_case_count: number
  settled_case_count: number
  failure_reason: string | null
  /** 非空意味着"轮到你了"（`05` §4.3）。**不靠猜 `phase` 字符串。** */
  interrupt: { kind: string } | null
}

export interface CaseItem {
  case_id: string
  name: string
  category: 'normal' | 'abnormal' | 'boundary'
  dimension: string
  confidence: 'high' | 'low' | null
  review_status: 'pending' | 'approved' | 'rejected'
  is_destructive: boolean
  request_method: string
  request_path: string
  assertions: Record<string, unknown>[]
}

export interface CasePage {
  run_id: string
  total: number
  limit: number
  offset: number
  items: CaseItem[]
}

export interface DecisionResponse {
  run_id: string
  applied: { approved: number; rejected: number }
}

const BASE = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    // `fetch` 只在**网络层**失败时 reject（连不上、被拦截）。
    // 4xx/5xx 不走这里——那些是我们能读出原因的响应。
    throw new ApiError(0, { type: 'network', title: '无法连接服务', status: 0, code: 'network' })
  }

  if (!response.ok) {
    let problem: ProblemDetail | undefined
    try {
      problem = (await response.json()) as ProblemDetail
    } catch {
      // 响应体不是 problem+json（中间有代理返回了 HTML 之类）。
      // 不在这里抛解析异常：`ApiError` 会给出一个带状态码的说明。
      problem = undefined
    }
    throw new ApiError(response.status, problem)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  uploadSpec(content: string, filename?: string): Promise<SpecCreated> {
    return request<SpecCreated>('/api/specs', {
      method: 'POST',
      body: JSON.stringify({ content, filename }),
    })
  },

  createRun(specId: string): Promise<{ run_id: string; phase: string }> {
    return request('/api/runs', { method: 'POST', body: JSON.stringify({ spec_id: specId }) })
  },

  getRun(runId: string): Promise<RunStatus> {
    return request<RunStatus>(`/api/runs/${encodeURIComponent(runId)}`)
  },

  getCases(runId: string, limit = 50, offset = 0): Promise<CasePage> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    return request<CasePage>(`/api/runs/${encodeURIComponent(runId)}/cases?${query}`)
  },

  submitDecisions(
    runId: string,
    decisions: { case_id: string; decision: 'approve' | 'reject' }[],
  ): Promise<DecisionResponse> {
    return request<DecisionResponse>(`/api/runs/${encodeURIComponent(runId)}/decisions`, {
      method: 'POST',
      body: JSON.stringify({ decisions }),
    })
  },

  /** 取 Markdown 形态的报告（写入时的快照，RA4）。 */
  async getReportMarkdown(runId: string): Promise<string> {
    const response = await fetch(`${BASE}/api/runs/${encodeURIComponent(runId)}/report`, {
      headers: { Accept: 'text/markdown' },
    })
    if (!response.ok) {
      let problem: ProblemDetail | undefined
      try {
        problem = (await response.json()) as ProblemDetail
      } catch {
        problem = undefined
      }
      throw new ApiError(response.status, problem)
    }
    return await response.text()
  },
}

/** 阶段的中文说明。**纯展示**——判断逻辑一律不依赖它。 */
export function describePhase(phase: string): string {
  const table: Record<string, string> = {
    init: '已创建',
    parsing: '解析文档',
    generating: '生成用例',
    awaiting_review: '等待人工确认',
    dispatching: '投递用例',
    awaiting_results: '等待执行结果',
    analyzing: '分析失败',
    reporting: '生成报告',
    done: '已完成',
    failed: '已失败',
    abandoned: '已放弃',
  }
  // 阶段枚举将来新增取值时，这里会显示原始字符串而不是空白——
  // 界面对一个不认识的阶段仍然要如实转达它看到了什么。
  return table[phase] ?? phase
}

export const TERMINAL_PHASES = new Set(['done', 'failed', 'abandoned'])
