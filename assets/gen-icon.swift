import AppKit
import CoreGraphics

// Regenerates assets/app-icon.icns with the classic Starfleet "Command
// division" insignia (TOS-era): gold delta with a black outline and a black
// 4-pointed star/flare emblem in the upper-middle, per the reference photo
// the user provided. Run via ./gen-icon.sh, not directly -- that script also
// does the sips/iconutil steps to produce the final .icns.
//
// Delta geometry transcribed from the community vector recreation at
// https://commons.wikimedia.org/wiki/File:Delta-shield.svg (its "ffb634"
// foreground path -- a single closed loop, tip at top, two lower "wing"
// points with a notch between). See StarfleetMark.swift for the plain
// silhouette version at menu-bar scale (no outline/star there -- that's a
// template image, single-color ink only, and adding fine inner detail at
// 13x20pt risked another round of the sizing/rendering issues already hit
// once this session).

let canvasSize: CGFloat = 1024

// Badge background: rounded square, dark -- evokes an actual Starfleet
// combadge sitting on a uniform, and (being fully opaque) reads fine in both
// light- and dark-mode Finder windows.
let badgeInset: CGFloat = 60
let badgeRect = CGRect(x: badgeInset, y: badgeInset,
                        width: canvasSize - badgeInset * 2, height: canvasSize - badgeInset * 2)
let badgeCornerRadius: CGFloat = badgeRect.width * 0.225
let badgeColor = NSColor(srgbRed: 0.043, green: 0.055, blue: 0.102, alpha: 1) // dark navy

// The delta mark, scaled to fit within the badge with generous padding,
// preserving its native aspect ratio (~97.69:156.44 wide:tall).
let markAspect: CGFloat = 97.69087 / 156.4396
let markHeight = badgeRect.height * 0.6
let markWidth = markHeight * markAspect
let markOriginX = badgeRect.midX - markWidth / 2
let markOriginY = badgeRect.midY - markHeight / 2
let s = markHeight / 156.4396 // scale factor from the source geometry's own units

let outlineColor = NSColor.black
let deltaColor = NSColor(srgbRed: 0.95, green: 0.80, blue: 0.20, alpha: 1) // command-division gold/yellow

// Source x is NOT zero-based: it runs from -50.34267 (left wing) to 47.3482
// (right wing), with the tip at x = 0. Subtract the left extent before scaling
// -- adding raw x to markOriginX (already the left edge of the centred box)
// pushed the whole delta half its own width off to the left.
let markMinX: CGFloat = -50.34267

func markPoint(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
    // Source coords have y growing downward from the shape's top (tip);
    // CoreGraphics grows y upward, so flip: y' = markOriginY + (156.4396 - y) * s
    CGPoint(x: markOriginX + (x - markMinX) * s, y: markOriginY + (156.4396 - y) * s)
}

/// The delta outline, built at `inset` from the true edge (in source units)
/// so drawing it twice -- once at inset 0 (black, full size) then again at a
/// positive inset (gold, slightly smaller) -- creates a uniform black border
/// without relying on CGContext stroke/miter joins on a curved path.
func deltaPath(inset: CGFloat) -> CGPath {
    let path = CGMutablePath()
    // Each anchor/control point nudged toward the shape's rough centroid
    // (0, 90) by `inset` units -- close enough for a thin uniform-looking
    // border at this scale; exact curve-offset math isn't needed here.
    func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        let cx: CGFloat = 0, cy: CGFloat = 90
        let dx = x - cx, dy = y - cy
        let len = max(sqrt(dx * dx + dy * dy), 1)
        let nx = x - dx / len * inset, ny = y - dy / len * inset
        return markPoint(nx, ny)
    }
    path.move(to: p(0, 0))
    path.addCurve(to: p(-50.34267, 156.4396), control1: p(-35.59494, 50.64347), control2: p(-48.41479, 99.37433))
    path.addCurve(to: p(12.12945, 96.79096), control1: p(-38.59728, 144.99238), control2: p(-1.2086, 99.43886))
    path.addCurve(to: p(47.3482, 140.96216), control1: p(21.03014, 95.0236), control2: p(28.85729, 105.76274))
    path.addCurve(to: p(0, 0), control1: p(42.85094, 91.46562), control2: p(22.28452, 32.41886))
    path.closeSubpath()
    return path
}

/// A symmetric N-pointed star (2N vertices, alternating outer/inner radius) --
/// the command-division emblem in the middle of the delta.
func starPath(center: CGPoint, outerRadius: CGFloat, innerRadius: CGFloat, points: Int) -> CGPath {
    let path = CGMutablePath()
    let step = CGFloat.pi / CGFloat(points)
    for i in 0..<(points * 2) {
        let radius = i % 2 == 0 ? outerRadius : innerRadius
        let angle = CGFloat(i) * step
        let point = CGPoint(x: center.x + radius * sin(angle), y: center.y + radius * cos(angle))
        if i == 0 { path.move(to: point) } else { path.addLine(to: point) }
    }
    path.closeSubpath()
    return path
}

guard let ctx = CGContext(
    data: nil, width: Int(canvasSize), height: Int(canvasSize),
    bitsPerComponent: 8, bytesPerRow: 0,
    space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else {
    print("failed to create context"); exit(1)
}

ctx.clear(CGRect(x: 0, y: 0, width: canvasSize, height: canvasSize))
let badgePath = CGPath(roundedRect: badgeRect, cornerWidth: badgeCornerRadius, cornerHeight: badgeCornerRadius, transform: nil)
ctx.addPath(badgePath)
ctx.setFillColor(badgeColor.cgColor)
ctx.fillPath()

// Black outline (full-size shape), then the gold fill slightly inset on top.
ctx.addPath(deltaPath(inset: 0))
ctx.setFillColor(outlineColor.cgColor)
ctx.fillPath()

ctx.addPath(deltaPath(inset: markHeight * 0.045 / s))
ctx.setFillColor(deltaColor.cgColor)
ctx.fillPath()

// The star emblem, upper-middle of the shape.
let starCenter = markPoint(-1, 88)
ctx.addPath(starPath(center: starCenter, outerRadius: markHeight * 0.11, innerRadius: markHeight * 0.045, points: 4))
ctx.setFillColor(outlineColor.cgColor)
ctx.fillPath()

guard let cgImage = ctx.makeImage() else { print("failed to make image"); exit(1) }
let rep = NSBitmapImageRep(cgImage: cgImage)
guard let data = rep.representation(using: .png, properties: [:]) else { print("failed to encode png"); exit(1) }
let outURL = URL(fileURLWithPath: CommandLine.arguments[1])
try data.write(to: outURL)
print("wrote \(outURL.path)")
