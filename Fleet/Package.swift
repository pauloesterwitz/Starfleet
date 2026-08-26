// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Fleet",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "Fleet"
        )
    ]
)
