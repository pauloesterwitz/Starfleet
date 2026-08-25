// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "OpencodeMonitor",
    platforms: [.macOS(.v13)],
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
