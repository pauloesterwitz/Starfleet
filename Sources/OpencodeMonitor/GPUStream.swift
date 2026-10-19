import Foundation

/// One GPU reading streamed from a Spark.
struct GPUSample {
    let utilPct: Double?
    let tempC: Double?
    let powerW: Double?
    let at: Date
}

/// The GPU figures of one Spark, streamed rather than polled.
///
/// WHY. The panel's GPU %, temperature and power used to come from the status
/// poller: one `ssh host opencode-status.py` every 2 s, ~0.8 s of work on Jean-Luc,
/// pushed to the panel on a second 2 s timer -- so a figure was 2-4 s old, and
/// polling faster would have kept ssh busy most of the time. GPU load is also very
/// bursty (0% and 47 C, then 96% and 68 C a minute later), so a sample every two
/// seconds mostly misses what happens.
///
/// HOW. One long-lived `ssh host nvidia-smi ... -lms 500`, which prints a line every
/// 500 ms with no per-sample process or handshake -- the same three fields, from the
/// same tool, as the host script reads, so the figures mean the same thing. RAM stays
/// on the slow poller: it moves slowly and `nvidia-smi` does not report it.
///
/// Only the panel reads this, deliberately NOT through @Published: the menu bar and
/// desktop widget observe the poller, and a publish twice a second would re-render
/// them for figures they do not show at that rate.
final class GPUStream: @unchecked Sendable {
    /// nvidia-smi's own loop. `nounits` so the lines are bare numbers.
    private static let remoteCommand =
        "nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw "
        + "--format=csv,noheader,nounits -lms 500"
    /// A sample older than this is as good as no sample: the stream has stalled.
    static let freshFor: TimeInterval = 3

    private let host: String
    private let lock = NSLock()
    private var process: Process?
    private var pending = Data()
    private var latestSample: GPUSample?
    private var wanted = false
    private var restartDelay: TimeInterval = 1
    /// Bumped on every start, so a late callback from an old process is ignored.
    private var generation = 0

    init(host: String) { self.host = host }
    deinit { stop() }

    /// The newest reading, or nil if there is none or it has gone stale.
    var fresh: GPUSample? {
        lock.lock(); defer { lock.unlock() }
        guard let sample = latestSample,
              Date().timeIntervalSince(sample.at) < Self.freshFor else { return nil }
        return sample
    }

    /// Begin streaming (idempotent). Restarts itself, with backoff, if ssh drops.
    func start() {
        lock.lock()
        guard !wanted else { lock.unlock(); return }
        wanted = true
        restartDelay = 1
        lock.unlock()
        launch()
    }

    /// Stop streaming and drop the reading, so nothing stale outlives it.
    func stop() {
        lock.lock()
        wanted = false
        generation += 1
        let running = process
        process = nil
        latestSample = nil
        pending = Data()
        lock.unlock()
        running?.terminate()
    }

    private func launch() {
        lock.lock()
        guard wanted, process == nil else { lock.unlock(); return }
        generation += 1
        let mine = generation
        lock.unlock()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        // Same options as StatusPoller.runSSH (see the note there on accept-new), plus
        // keepalives: a stream that goes quiet because the network dropped has to be
        // noticed and replaced, not waited on forever.
        process.arguments = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                             "-o", "StrictHostKeyChecking=accept-new",
                             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                             host, Self.remoteCommand]
        let out = Pipe()
        process.standardOutput = out
        // Not surfaced: StatusPoller owns the "Needs Tailscale re-auth" UI, and a
        // second, noisier channel for the same condition would only fight it.
        process.standardError = FileHandle.nullDevice
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }   // EOF
            self?.ingest(data, generation: mine)
        }
        process.terminationHandler = { [weak self] _ in self?.exited(generation: mine) }

        lock.lock()
        guard wanted, generation == mine else { lock.unlock(); return }
        self.process = process
        lock.unlock()
        do { try process.run() } catch { exited(generation: mine) }
    }

    private func ingest(_ data: Data, generation mine: Int) {
        lock.lock(); defer { lock.unlock() }
        guard generation == mine else { return }
        pending.append(data)
        while let newline = pending.firstIndex(of: UInt8(ascii: "\n")) {
            let line = String(decoding: pending[pending.startIndex..<newline], as: UTF8.self)
            pending.removeSubrange(pending.startIndex...newline)
            if let sample = Self.parse(line) {
                latestSample = sample
                restartDelay = 1          // it is working: forget any backoff
            }
        }
    }

    private func exited(generation mine: Int) {
        lock.lock()
        guard generation == mine else { lock.unlock(); return }
        process = nil
        let again = wanted
        let delay = restartDelay
        restartDelay = min(restartDelay * 2, 15)
        lock.unlock()
        guard again else { return }
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.launch()
        }
    }

    /// "96, 68, 49.99" -> a sample. A field the driver cannot report comes back as
    /// "[N/A]" (or "N/A"), which becomes nil so the caller can fall back per field.
    /// Anything that is not three comma-separated fields is not a sample at all.
    static func parse(_ line: String, at date: Date = Date()) -> GPUSample? {
        let fields = line.split(separator: ",", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard fields.count == 3 else { return nil }
        let numbers = fields.map { Double($0) }
        guard numbers.contains(where: { $0 != nil }) else { return nil }
        return GPUSample(utilPct: numbers[0], tempC: numbers[1], powerW: numbers[2], at: date)
    }
}
