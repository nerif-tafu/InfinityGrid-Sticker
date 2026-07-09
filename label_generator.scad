///////////////////////////////////////////////
// InfinityGrid Label Generator             //
// Imports SVG designs onto label bases     //
///////////////////////////////////////////////

/* [Label Settings] */
// SVG file to import (export from web app)
svg_file = "example.svg"; // [.svg file]

// Label size units (must match SVG export)
Y_units = 1; // [0.3, 0.5, 1, 2, 3]

// Label base color
Label_color = "#000000"; // color

// Content/design color (raised part)
Content_color = "#FFFFFF"; // color

/* [Advanced Settings] */
// Base width (mm)
base_width = 11.5;

// Base height (mm)
base_height = 0.8;

// Corner radius (mm)
corner_radius = 0.9;

// Edge chamfer (mm)
chamfer = 0.2;

// Content extrusion height (mm) - 0.2 for raised, 0.01 for flush
content_height = 0.2;

// Mounting hole diameter (mm)
hole_diameter = 1.5;

/* [Hidden] */
$fs = 0.1;
$fa = 5;

// Calculate label length based on units
function get_length(units) =
    (units == 0.3) ? 12.8 :
    (units == 0.5) ? 18.55 :
    (units == 1) ? 35.8 :
    (units == 2) ? 77.8 :
    (units == 3) ? 119.8 : 35.8;

// SVG dimensions (must match export settings)
function get_svg_width(units) =
    (units == 0.3) ? 11.5 :
    (units == 0.5) ? 17.25 :
    (units == 1) ? 34.5 :
    (units == 2) ? 76.5 :
    (units == 3) ? 118.5 : 34.5;

svg_height = 10.5;

length = get_length(Y_units);
svg_width = get_svg_width(Y_units);


///////////////////////////////////////////////
//              MAIN MODULE                  //
///////////////////////////////////////////////

// Generate the label
label();

module label() {
    // Base
    color(Label_color) {
        difference() {
            labelbase(length, base_width, base_height, corner_radius, chamfer);

            // Mounting holes at each end
            translate([(length - 1) / 2, 0, 0])
                cylinder(h = base_height + 1, d = hole_diameter, center = true);

            translate([(-length + 1) / 2, 0, 0])
                cylinder(h = base_height + 1, d = hole_diameter, center = true);
        }
    }

    // SVG content
    color(Content_color) {
        translate([0, 0, base_height])
            import_svg_centered();
    }
}


///////////////////////////////////////////////
//           SVG IMPORT MODULE               //
///////////////////////////////////////////////

module import_svg_centered() {
    // Center the SVG on the label
    translate([-svg_width / 2, -svg_height / 2, 0]) {
        linear_extrude(height = content_height) {
            import(svg_file, center = false);
        }
    }
}


///////////////////////////////////////////////
//         LABEL BASE MODULES               //
///////////////////////////////////////////////

module labelbase(length, width, height, radius, champ) {
    // Extra perimeter shape (thin border)
    translate([(-length - 2) / 2, -5.7 / 2, 0]) {
        shape_with_chamfer(length + 2, 5.7, height, 0.2, champ);
    }

    // Main label shape
    translate([(-length) / 2, -width / 2, 0]) {
        shape_with_chamfer(length, width, height, radius, champ);
    }
}

module shape_with_chamfer(length, width, height, radius, champ) {
    // Bottom chamfer
    chamfer_layer(length, width, champ, radius, flip = false);

    // Main body
    translate([0, 0, champ])
        rounded_rect(length, width, height - 2 * champ, radius);

    // Top chamfer
    translate([0, 0, height - champ])
        chamfer_layer(length, width, champ, radius, flip = true);
}

module chamfer_layer(length, width, size, radius, flip = false) {
    r1 = flip ? radius : radius - size;
    r2 = flip ? radius - size : radius;

    hull() {
        translate([radius, radius, 0])
            cylinder(h = size, r1 = r1, r2 = r2);
        translate([radius, width - radius, 0])
            cylinder(h = size, r1 = r1, r2 = r2);
        translate([length - radius, width - radius, 0])
            cylinder(h = size, r1 = r1, r2 = r2);
        translate([length - radius, radius, 0])
            cylinder(h = size, r1 = r1, r2 = r2);
    }
}

module rounded_rect(length, width, height, radius) {
    hull() {
        translate([radius, radius, 0])
            cylinder(h = height, r = radius);
        translate([radius, width - radius, 0])
            cylinder(h = height, r = radius);
        translate([length - radius, width - radius, 0])
            cylinder(h = height, r = radius);
        translate([length - radius, radius, 0])
            cylinder(h = height, r = radius);
    }
}


///////////////////////////////////////////////
//            BATCH EXPORT                   //
///////////////////////////////////////////////

// To batch export multiple labels:
// 1. Export all your designs as SVG from the web app
// 2. Uncomment the batch_labels module below
// 3. Update the file list
// 4. Render and export as STL

/*
batch_files = [
    "label1.svg",
    "label2.svg",
    "label3.svg"
];

module batch_labels() {
    spacing = length + 3;

    for (i = [0 : len(batch_files) - 1]) {
        translate([0, i * -15, 0]) {
            // Temporarily override svg_file
            // Note: OpenSCAD doesn't support dynamic file names easily
            // You may need to generate separate .scad files for each
        }
    }
}
*/
