import SwiftUI

@main
struct FleetApp: App {
    var body: some Scene {
        WindowGroup("Fleet") {
            ContentView()
                .frame(minWidth: 960, minHeight: 640)
        }
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) { }
            CommandGroup(after: .toolbar) {
                Button("Reload Fleet") {
                    NotificationCenter.default.post(name: .fleetReload, object: nil)
                }
                .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}
