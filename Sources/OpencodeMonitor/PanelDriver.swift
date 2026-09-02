import Foundation
import IOKit.hid

/// Talks to the 3.5" llhmi.com / llTechCo USB panel (0483:0065) over raw HID.
///
/// The panel is NOT a display -- it is a full-speed HID device that stores a
/// compiled theme and renders locally, while the host pushes numeric values by
/// channel id. Protocol transcribed from James Buren's `hidss` reference
/// (gitlab.com/braewoods/usb-smart-screen, branch `hidss`); that implementation
/// drives Linux /dev/hidraw, so the transport here is IOHIDManager instead.
///
/// Wire format: 64-byte output reports, no report IDs (the descriptor declares
/// a single vendor-defined 0xFF00 collection with 64-byte in/out). Byte 0
/// selects the report type.
final class PanelDriver {
    static let vendorID = 0x0483
    static let productID = 0x0065
    private static let reportSize = 64
    /// A 64-byte report holds `[type][count]` plus 20 x `[channel][u16]`, and
    /// the firmware caps a sensor report at 20 pairs for the same reason.
    private static let maxPairsPerReport = 20

    private enum ReportType: UInt8 {
        case widget = 0x00
        case command = 0x01
        case sensor = 0x02
        case datetime = 0x03
    }

    private var manager: IOHIDManager?
    private var device: IOHIDDevice?

    var isOpen: Bool { device != nil }

    // MARK: - Connection

    @discardableResult
    func open() -> Bool {
        if device != nil { return true }

        let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
        let matching: [String: Any] = [
            kIOHIDVendorIDKey as String: Self.vendorID,
            kIOHIDProductIDKey as String: Self.productID,
        ]
        IOHIDManagerSetDeviceMatching(manager, matching as CFDictionary)
        guard IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone)) == kIOReturnSuccess,
              let devices = IOHIDManagerCopyDevices(manager) as? Set<IOHIDDevice>,
              let found = devices.first
        else {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            return false
        }
        guard IOHIDDeviceOpen(found, IOOptionBits(kIOHIDOptionsTypeNone)) == kIOReturnSuccess else {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            return false
        }

        self.manager = manager
        self.device = found
        return true
    }

    func close() {
        if let device {
            IOHIDDeviceClose(device, IOOptionBits(kIOHIDOptionsTypeNone))
        }
        if let manager {
            IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
        }
        device = nil
        manager = nil
    }

    deinit { close() }

    // MARK: - Reports

    /// Sends one 64-byte output report. Report ID is 0 because the descriptor
    /// declares none -- the payload itself carries the type in byte 0.
    @discardableResult
    private func write(_ payload: [UInt8]) -> Bool {
        guard let device else { return false }
        var buffer = payload
        buffer.append(contentsOf: repeatElement(0, count: max(0, Self.reportSize - buffer.count)))
        let result = IOHIDDeviceSetReport(
            device, kIOHIDReportTypeOutput, 0, buffer, Self.reportSize
        )
        if result != kIOReturnSuccess {
            // Almost always the panel being unplugged; drop the handle so the
            // next push re-opens rather than writing into a dead device forever.
            close()
            return false
        }
        return true
    }

    /// `[type][count]` then `count` x `[channel][value big-endian u16]`.
    /// The firmware accepts at most 20 pairs, which is also what fits in 64 bytes.
    @discardableResult
    private func writePairs(_ type: ReportType, _ pairs: [(UInt8, UInt16)]) -> Bool {
        precondition(pairs.count <= Self.maxPairsPerReport, "at most 20 pairs per report")
        var payload: [UInt8] = [type.rawValue, UInt8(pairs.count)]
        for (channel, value) in pairs {
            payload.append(channel)
            payload.append(UInt8(value >> 8))
            payload.append(UInt8(value & 0xFF))
        }
        return write(payload)
    }

    /// Pushes live values. Channel ids are arbitrary numeric slots -- the loaded
    /// theme decides where each is drawn and what it is labelled, so the
    /// firmware's own names for them (cpu_temperature etc.) are irrelevant.
    ///
    /// The theme has more channels than one report can carry, so a full push is
    /// split across reports. There is no begin/commit around a sensor write --
    /// the firmware applies each pair as it arrives -- so the only consequence
    /// is that the last fields land a fraction of a millisecond after the
    /// first, which is invisible at a 2s cadence.
    @discardableResult
    func sendSensors(_ values: [UInt8: UInt16]) -> Bool {
        let pairs = values.sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
        guard !pairs.isEmpty else { return true }
        for start in stride(from: 0, to: pairs.count, by: Self.maxPairsPerReport) {
            let end = min(start + Self.maxPairsPerReport, pairs.count)
            // Bail on the first failure: write() has already closed the handle,
            // so the rest would only queue up writes into a dead device.
            guard writePairs(.sensor, Array(pairs[start..<end])) else { return false }
        }
        return true
    }

    /// Also carries the backlight setting, which is the one field that works
    /// regardless of what the loaded theme draws -- handy as a proof of life.
    /// `timeout` is in 1/8 s; under a second disables blanking entirely.
    @discardableResult
    func sendDateTime(_ when: Date = Date(), brightness: UInt8 = 100, timeout: UInt8 = 0) -> Bool {
        let parts = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: when
        )
        let payload: [UInt8] = [
            ReportType.datetime.rawValue,
            0x01,                                   // number of fields
            0x15,                                   // field 21 = datetime + backlight
            UInt8(clamping: (parts.year ?? 2000) - 2000),
            UInt8(clamping: parts.month ?? 1),
            UInt8(clamping: parts.day ?? 1),
            UInt8(clamping: parts.hour ?? 0),
            UInt8(clamping: parts.minute ?? 0),
            UInt8(clamping: parts.second ?? 0),
            timeout,
            max(1, min(100, brightness)),
        ]
        return write(payload)
    }
}
