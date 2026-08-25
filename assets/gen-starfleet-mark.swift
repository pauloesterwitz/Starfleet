import AppKit
import CoreGraphics

// Regenerates the menu-bar-scale bitmap embedded in StarfleetMark.swift.
// Run directly with `swift gen-starfleet-mark.swift <output.png>`, then
// base64-encode the result and paste it into StarfleetMark.swift's `base64`
// constant if the mark's size/geometry ever needs to change.
//
// Geometry transcribed from the community vector recreation at
// https://commons.wikimedia.org/wiki/File:Delta-shield.svg (its "ffb634"
// foreground path -- a single closed loop, tip at top, two lower "wing"
// points with a notch between). Coords shifted so min-x=0/min-y=0, scaled to
// an 80pt-tall canvas, y-flipped for CoreGraphics. See assets/gen-icon.swift
// for the same geometry at app-icon scale (with an added dark badge and gold
// fill, since that one isn't a template image).

let w: CGFloat = 50, h: CGFloat = 80

let path = CGMutablePath()
path.move(to: CGPoint(x: 25.744, y: 80))
path.addCurve(to: CGPoint(x: 0, y: 0), control1: CGPoint(x: 7.542, y: 54.103), control2: CGPoint(x: 0.986, y: 29.184))
path.addCurve(to: CGPoint(x: 31.949, y: 30.501), control1: CGPoint(x: 6.006, y: 5.85), control2: CGPoint(x: 25.126, y: 29.151))
path.addCurve(to: CGPoint(x: 49.966, y: 7.904), control1: CGPoint(x: 36.499, y: 31.402), control2: CGPoint(x: 40.502, y: 25.914))
path.addCurve(to: CGPoint(x: 25.744, y: 80), control1: CGPoint(x: 47.658, y: 33.220), control2: CGPoint(x: 37.140, y: 63.422))
path.closeSubpath()

guard let ctx = CGContext(
    data: nil, width: Int(w), height: Int(h),
    bitsPerComponent: 8, bytesPerRow: 0,
    space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else { exit(1) }

ctx.clear(CGRect(x: 0, y: 0, width: w, height: h))
ctx.setFillColor(NSColor.black.cgColor) // template image ink -- macOS re-tints for light/dark menu bar
ctx.addPath(path)
ctx.fillPath()

guard let cgImage = ctx.makeImage() else { exit(1) }
let rep = NSBitmapImageRep(cgImage: cgImage)
let data = rep.representation(using: .png, properties: [:])!
try data.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
print("wrote \(CommandLine.arguments[1])")
