import Foundation
import SwiftUI

struct LlamaSwapModel: Codable {
    let model: String?
    let state: String?
    let cluster: Bool

    enum CodingKeys: String, CodingKey {
        case model, state, cluster
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        state = try c.decodeIfPresent(String.self, forKey: .state)
        // Absent on a host not yet redeployed past the cluster-tagging change.
        cluster = try c.decodeIfPresent(Bool.self, forKey: .cluster) ?? false
    }
}

struct LlamaSwapStatus: Codable {
    let reachable: Bool
    let models: [LlamaSwapModel]

    static let unavailable = LlamaSwapStatus(reachable: false, models: [])
}

/// Cluster models (llama-swap's `cluster: true` tag) occupy BOTH Sparks at
/// once, but llama-swap itself only runs on one host today -- so its
/// /running report is the only source for these. This folds a cluster
/// model reported by `other` into `own`'s list too (if not already there),
/// so it shows under every Starfleet member it actually occupies rather
/// than just the one whose llama-swap happened to answer. Regular
/// (non-cluster) models are never borrowed this way -- those genuinely run
/// on one machine only.
func effectiveLlamaSwapModels(own: LlamaSwapStatus, other: LlamaSwapStatus) -> [LlamaSwapModel] {
    var seen = Set(own.models.compactMap(\.model))
    var models = own.models
    for m in other.models where m.cluster {
        guard let name = m.model, !seen.contains(name) else { continue }
        models.append(m)
        seen.insert(name)
    }
    return models
}

struct HostStats: Codable {
    let ramUsedGB: Double
    let ramTotalGB: Double
    let ramPct: Double
    let swapUsedGB: Double
    let swapTotalGB: Double
    let gpuUtilPct: Double?
    let gpuPowerW: Double?
    let gpuTempC: Double?
    let llamaSwap: LlamaSwapStatus

    enum CodingKeys: String, CodingKey {
        case ramUsedGB = "ram_used_gb"
        case ramTotalGB = "ram_total_gb"
        case ramPct = "ram_pct"
        case swapUsedGB = "swap_used_gb"
        case swapTotalGB = "swap_total_gb"
        case gpuUtilPct = "gpu_util_pct"
        case gpuPowerW = "gpu_power_w"
        case gpuTempC = "gpu_temp_c"
        case llamaSwap = "llama_swap"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ramUsedGB = try c.decode(Double.self, forKey: .ramUsedGB)
        ramTotalGB = try c.decode(Double.self, forKey: .ramTotalGB)
        ramPct = try c.decode(Double.self, forKey: .ramPct)
        swapUsedGB = try c.decode(Double.self, forKey: .swapUsedGB)
        swapTotalGB = try c.decode(Double.self, forKey: .swapTotalGB)
        gpuUtilPct = try c.decodeIfPresent(Double.self, forKey: .gpuUtilPct)
        gpuPowerW = try c.decodeIfPresent(Double.self, forKey: .gpuPowerW)
        gpuTempC = try c.decodeIfPresent(Double.self, forKey: .gpuTempC)
        // A host still running a pre-llama-swap-awareness opencode-status.py
        // never emits this key -- default to "unavailable" rather than
        // failing to decode the whole payload over one field.
        llamaSwap = try c.decodeIfPresent(LlamaSwapStatus.self, forKey: .llamaSwap) ?? .unavailable
    }
}

struct TodoCounts: Codable {
    let pending: Int
    let inProgress: Int
    let completed: Int

    enum CodingKeys: String, CodingKey {
        case pending, completed
        case inProgress = "in_progress"
    }
}

struct SessionStatus: Codable, Identifiable {
    let id: String
    let title: String
    let directory: String
    let updatedSecsAgo: Double
    let todos: TodoCounts
    let pid: Int?
    let cpuPct: Double
    let status: String
    let stalled: Bool
    let generating: Bool
    let tool: String

    enum CodingKeys: String, CodingKey {
        case id, title, directory, todos, pid, status, stalled, generating, tool
        case updatedSecsAgo = "updated_secs_ago"
        case cpuPct = "cpu_pct"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decode(String.self, forKey: .title)
        directory = try c.decode(String.self, forKey: .directory)
        updatedSecsAgo = try c.decode(Double.self, forKey: .updatedSecsAgo)
        todos = try c.decode(TodoCounts.self, forKey: .todos)
        pid = try c.decodeIfPresent(Int.self, forKey: .pid)
        cpuPct = try c.decode(Double.self, forKey: .cpuPct)
        status = try c.decode(String.self, forKey: .status)
        stalled = try c.decode(Bool.self, forKey: .stalled)
        generating = try c.decode(Bool.self, forKey: .generating)
        // A host still running the pre-Claude-Code opencode-status.py (e.g.
        // Jean-Luc, offline as of this field's introduction) never emits this
        // key at all -- default to the only tool that existed before now,
        // rather than failing to decode the whole payload over one field.
        tool = try c.decodeIfPresent(String.self, forKey: .tool) ?? "opencode"
    }

    // Purely recency-based, not `status` (which came from opencode's own
    // cpu_pct/todo heuristic and doesn't exist in a comparable form for
    // Claude Code sessions) -- one rule that reads the same for both tools.
    var badgeColor: Color {
        if updatedSecsAgo < 30 * 60 { return .green }
        if updatedSecsAgo < 8 * 3600 { return .orange }
        return .gray
    }

    // `status=="working"` is derived from cpu_pct, which match_pid() can
    // misattribute from a sibling process sharing the session's directory --
    // it over-reports. `generating` (opencode-status.py) instead checks
    // whether the session's LATEST message is an assistant reply with no
    // completion timestamp yet -- ground truth for "actually mid-generation
    // right now", not just "this directory has some active process".
    var isProcessing: Bool { generating }
}

private let maxSessionAgeSecs: Double = 24 * 3600 // 1 day

extension [SessionStatus] {
    /// Never shown in either UI surface once idle this long -- a display-layer
    /// rule enforced here regardless of what the remote script itself already
    /// filters to (opencode-status.py has its own ~24h RECENT_WINDOW_SECS, but
    /// that governs what's worth fetching/computing at all, not what's allowed
    /// on screen; keeping the UI's own cutoff means it holds even if that
    /// window is ever widened or a payload includes something stale).
    var recentEnoughToShow: [SessionStatus] { filter { $0.updatedSecsAgo <= maxSessionAgeSecs } }

    /// Currently-processing sessions first, so an active session never gets
    /// buried -- or truncated out of a capped list -- behind idle/waiting
    /// ones; newest-updated first within each of those two groups.
    var processingFirst: [SessionStatus] {
        let byRecency = sorted { $0.updatedSecsAgo < $1.updatedSecsAgo }
        return byRecency.filter(\.isProcessing) + byRecency.filter { !$0.isProcessing }
    }

    /// Total actively-generating sessions, for the menu bar's aggregate "⚡ N".
    var processingCount: Int { lazy.filter(\.isProcessing).count }

    /// Per-host UI splits sessions into these two groups -- Claude Code and
    /// opencode are entirely separate tools/data sources (JSONL transcripts
    /// vs. a SQLite DB), so a merged list would mix two different session
    /// lifecycles under one heading.
    var claudeCodeSessions: [SessionStatus] { filter { $0.tool == "claude-code" } }
    var opencodeSessions: [SessionStatus] { filter { $0.tool == "opencode" } }
}

struct StatusPayload: Codable {
    let host: HostStats
    let sessions: [SessionStatus]
    let error: String?
}

struct DetailPart: Codable, Identifiable {
    let type: String
    let text: String?
    let tool: String?
    let status: String?
    let title: String?
    let filename: String?

    var id: String { "\(type)-\(text ?? "")-\(tool ?? "")-\(filename ?? "")" }
}

struct DetailMessage: Codable, Identifiable {
    let role: String
    let created: Double?
    let completed: Double?
    let finish: String?
    let parts: [DetailPart]

    var id: String { "\(role)-\(created ?? 0)" }
}

struct SessionDetail: Codable {
    let sessionID: String?
    let title: String?
    let messages: [DetailMessage]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case title, messages, error
    }
}
