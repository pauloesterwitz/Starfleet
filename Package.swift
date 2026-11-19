// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "OpencodeMonitor",
    // 15: the Fleet window's full screen needs SwiftUI's windowManagerRole (App.swift). No
    // real limit: building needs Xcode with the macOS 27 SDK, which needs a newer macOS anyway.
    platforms: [.macOS("15.0")],
    targets: [
        .executableTarget(
            name: "OpencodeMonitor",
            linkerSettings: [
                // Embeds Info.plist (LSUIElement) directly into the binary so the
                // Dock icon never appears -- no .xcodeproj/.app bundle needed.
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/OpencodeMonitor/Info.plist",
                ])
            ]
        )
    ]
)
