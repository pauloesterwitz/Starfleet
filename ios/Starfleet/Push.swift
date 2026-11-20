import Foundation
import UIKit
import UserNotifications
import WebKit

/// Messages from Fleet on the phone: notification permission, the device token, a tap on a
/// notification, the icon badge. The page asks through PaddBridge ("push-status", "push-enable",
/// "push-disable", "badge") and is answered through PADD.pushState / PADD.pushToken / PADD.open.
///
/// Nothing here needs the push entitlement to build or run. On a free Apple team registering for
/// remote notifications fails; that is reported to the page as the error "entitlement". The system's
/// permission prompt only ever follows a tap on "Turn on push" ("push-enable"), never the launch.
@MainActor
final class PaddPush {
    static let shared = PaddPush()

    /// Set where the web view goes on screen (FleetWebView.makeUIView), not where it is made: the
    /// screen's @State makes a new one each time the screen is rebuilt and keeps only the first.
    weak var webView: WKWebView?

    private static let wantedKey = "pushWanted"
    private var pageIsReady = false
    private var pendingGo: String?
    private var token: String?
    private var problem = ""
    private var refreshed = false

    // MARK: From the page

    func handle(_ type: String, _ n: Int) {
        switch type {
        case "push-status": status()
        case "push-enable": enable()
        case "push-disable": disable()
        case "badge": badge(n)
        default: break
        }
    }

    /// A page is loading: nothing is passed to it until it sends "ready" again.
    func pageLoading() { pageIsReady = false }

    func pageReady() {
        pageIsReady = true
        if let go = pendingGo {
            pendingGo = nil
            call("open", [go])
        }
    }

    /// A tapped notification: open its screen, now if the page is ready, otherwise once it says so.
    /// "Ready" can be stale (iOS ended the page's process in the background and the page is not back yet), so
    /// the page's own answer decides: PADD.open returns true when it opened the route. Anything else, an error
    /// or false, keeps the route for the next "ready".
    func route(_ go: String) {
        guard go.hasPrefix("#/"), go.count <= 200 else { return }
        guard pageIsReady, let webView, let script = PushText.script("open", [go]) else {
            pendingGo = go
            return
        }
        webView.evaluateJavaScript(script) { [weak self] result, error in
            let opened = error == nil && (result as? Bool) == true
            if !opened {
                Task { @MainActor in self?.pendingGo = go }
            }
        }
    }

    private func status() {
        Task {
            // Once per launch, for someone who turned push on: register again. The token can change,
            // and a stale one loses messages without any sign.
            if await currentPermission() == "granted", !refreshed, UserDefaults.standard.bool(forKey: Self.wantedKey) {
                refreshed = true
                UIApplication.shared.registerForRemoteNotifications()
            }
            await sendState()
        }
    }

    private func enable() {
        UserDefaults.standard.set(true, forKey: Self.wantedKey)
        problem = ""
        Task {
            let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
            if granted { UIApplication.shared.registerForRemoteNotifications() }
            await sendState()
        }
    }

    private func disable() {
        UserDefaults.standard.set(false, forKey: Self.wantedKey)
        UIApplication.shared.unregisterForRemoteNotifications()
        token = nil
        problem = ""
        Task { await sendState() }
    }

    /// The icon badge. Without permission the system refuses; that is ignored.
    private func badge(_ n: Int) {
        Task { _ = try? await UNUserNotificationCenter.current().setBadgeCount(max(0, n)) }
    }

    // MARK: From iOS

    func didRegister(_ deviceToken: Data) {
        let hex = PushText.hex(deviceToken)
        token = hex
        problem = ""
        call("pushToken", [hex, PushText.environment()])
        Task { await sendState() }
    }

    func didFail(_ text: String) {
        problem = text
        Task { await sendState() }
    }

    // MARK: To the page

    private func currentPermission() async -> String {
        switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
        case .authorized, .provisional, .ephemeral: return "granted"
        case .denied: return "denied"
        default: return "undetermined"
        }
    }

    private func sendState() async {
        let permission = await currentPermission()
        let state: [String: Any] = ["permission": permission, "registered": token != nil, "error": problem]
        call("pushState", [state])
    }

    private func call(_ fn: String, _ args: [Any]) {
        guard let script = PushText.script(fn, args) else { return }
        webView?.evaluateJavaScript(script, completionHandler: nil)
    }
}

/// Launch, the device token, and notifications. Added to the app with @UIApplicationDelegateAdaptor.
@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    /// The delegate must be set before launch ends, or a tap that starts the app is lost.
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PaddPush.shared.didRegister(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PaddPush.shared.didFail(PushText.errorText(error))
    }

    // The two callbacks below can arrive off the main thread: nonisolated, and the work hops to it.

    /// While the app is open the page shows its own toast: no banner. The one exception is the test push
    /// ("Send a test" adds "test": true to its payload), which shows, so the test proves that push arrived.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let isTest = notification.request.content.userInfo["test"] as? Bool ?? false
        completionHandler(isTest ? [.banner, .list, .sound] : [])
    }

    /// A tap: the payload's "go" is a screen of the page, e.g. "#/models".
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let go = response.notification.request.content.userInfo["go"] as? String
        let tapped = response.actionIdentifier == UNNotificationDefaultActionIdentifier
        completionHandler()
        if tapped, let go {
            Task { @MainActor in PaddPush.shared.route(go) }
        }
    }
}
