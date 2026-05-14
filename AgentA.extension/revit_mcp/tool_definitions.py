# -*- coding: utf-8 -*-

TOOL_DECLARATIONS = [
    {
        "function_declarations": [
            {
                "name": "get_document_info",
                "description": "Get current Revit project details."
            },
            {
                "name": "create_wall",
                "description": "Create a wall. Specify length or use end_x/end_y for precise placement.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "length_mm": {"type": "number"},
                        "height_mm": {"type": "number"},
                        "start_x": {"type": "number"},
                        "start_y": {"type": "number"},
                        "end_x": {"type": "number"},
                        "end_y": {"type": "number"},
                        "thickness_mm": {"type": "number"},
                        "level_name": {"type": "string", "description": "Optional level name or ID (e.g., 'Level 2')."}
                    }
                }
            },
            {
                "name": "list_elements",
                "description": "List all elements in a category (wall, floor, level) to see their IDs and spatial locations (bounding boxes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Category to list: 'wall', 'floor', or 'level'."}
                    },
                    "required": ["category"]
                }
            },
            {
                "name": "get_element_details",
                "description": "Get geometric details (lines/points) of a Revit element by ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "orchestrate_build",
                "description": "MANDATORY: Use this for ALL building-wide requests: creating, editing, resizing, or ADDING/REMOVING/INSERTING storeys in multi-story buildings. Works from a single high-level prompt. Do NOT ask for IDs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "The full building request, e.g. 'create a 10 storey 30x40m building 3m floor-to-floor' or 'edit the building to 20m x 25m'."}
                    },
                    "required": ["prompt"]
                }
            },
            {
                "name": "edit_entire_building_dimensions",
                "description": "STATE-AWARE SYNC: Instantly update the entire building (footprint, height, storeys) OR specific individual floors. Use this for ANY 'edit', 'resize', 'add floor', 'remove floor', or 'remodel' request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "width_mm": {"type": "number", "description": "New building width in mm. Pass 0 if only editing specific floor overrides."},
                        "depth_mm": {"type": "number", "description": "New building depth in mm. Pass 0 if only editing specific floor overrides."},
                        "height_mm": {"type": "number", "description": "Total building height in mm. Pass 0 if only editing specific floor overrides."},
                        "column_span_mm": {"type": "number", "description": "Required column span in mm (Rule: centered grid, max 1/3 cantilever)."},
                        "advanced_instructions": {"type": "string", "description": "Pass any floor-specific overrides or constraints requested by the user here."}
                    },
                    "required": []
                }
            },
            {
                "name": "delete_walls",
                "description": "Delete all walls."
            },
            {
                "name": "move_element",
                "description": "Move any element by offset or cardinal direction (North, South, East, West).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "dx_mm": {"type": "number"},
                        "dy_mm": {"type": "number"},
                        "dz_mm": {"type": "number"},
                        "direction": {"type": "string", "description": "North (+Y), South (-Y), East (+X), West (-X)"},
                        "distance_mm": {"type": "number"}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "move_staircase",
                "description": "Move a specific staircore (all its walls, floors) to a new location, checking the 60m travel rule first. If the new location fails the 60m rule, it suggests a nearby valid location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "stair_idx": {"type": "integer", "description": "The sequential index of the staircore (e.g. 1, 2, 3)."},
                        "target_x_mm": {"type": "number", "description": "Target center X coordinate in mm."},
                        "target_y_mm": {"type": "number", "description": "Target center Y coordinate in mm."}
                    },
                    "required": ["stair_idx", "target_x_mm", "target_y_mm"]
                }
            },
            {
                "name": "create_floor",
                "description": "Create a rectangular floor.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "width_mm": {"type": "number"},
                        "length_mm": {"type": "number"},
                        "center_x": {"type": "number"},
                        "center_y": {"type": "number"},
                        "level_name": {"type": "string", "description": "Optional level name or ID."}
                    },
                    "required": ["width_mm", "length_mm"]
                }
            },
            {
                "name": "set_parameter",
                "description": "Set a parameter on an element (e.g., 'Room Bounding', 'Structural', 'Base Offset'). Values are strings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "parameter_name": {"type": "string"},
                        "value": {"type": "string"}
                    },
                    "required": ["element_id", "parameter_name", "value"]
                }
            },
            {
                "name": "query_types",
                "description": "List available 'wall' or 'floor' types.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"}
                    },
                    "required": ["category"]
                }
            },
            {
                "name": "create_polygon_floor",
                "description": "Create a floor from a list of X/Y points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "points_mm": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number"},
                                    "y": {"type": "number"}
                                }
                            }
                        },
                        "level_name": {"type": "string", "description": "Optional level name or ID."}
                    },
                    "required": ["points_mm"]
                }
            },
            {
                "name": "delete_element",
                "description": "Delete a specific element by ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "create_level",
                "description": "Create a new level at a specific elevation (mm).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "elevation_mm": {"type": "number"}
                    },
                    "required": ["elevation_mm"]
                }
            },
            {
                "name": "create_grid",
                "description": "Create a linear grid between two points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number"},
                        "y1": {"type": "number"},
                        "x2": {"type": "number"},
                        "y2": {"type": "number"}
                    },
                    "required": ["x1", "y1", "x2", "y2"]
                }
            },
            {
                "name": "create_arc_grid",
                "description": "Create a curved grid using start, end, and middle points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_x": {"type": "number"},
                        "start_y": {"type": "number"},
                        "end_x": {"type": "number"},
                        "end_y": {"type": "number"},
                        "mid_x": {"type": "number"},
                        "mid_y": {"type": "number"}
                    },
                    "required": ["start_x", "start_y", "end_x", "end_y", "mid_x", "mid_y"]
                }
            },
            {
                "name": "edit_grid",
                "description": "Rename or change the type of a grid line.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "grid_id": {"type": "string"},
                        "name": {"type": "string"},
                        "type_name": {"type": "string"}
                    },
                    "required": ["grid_id"]
                }
            },
            {
                "name": "create_wall_type",
                "description": "Create a new wall type with name and thickness.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "thickness_mm": {"type": "number"}
                    },
                    "required": ["name", "thickness_mm"]
                }
            },
            {
                "name": "create_floor_type",
                "description": "Create a new floor type with name and thickness.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "thickness_mm": {"type": "number"}
                    },
                    "required": ["name", "thickness_mm"]
                }
            },
            {
                "name": "create_column",
                "description": "Create an architectural column at a level and location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type_name": {"type": "string", "description": "Partial or full name of the column type (e.g., '610 x 610mm')."},
                        "level_name": {"type": "string", "description": "Level name (e.g., 'Level 1')."},
                        "x": {"type": "number", "description": "X coordinate in mm."},
                        "y": {"type": "number", "description": "Y coordinate in mm."},
                        "rotation_degrees": {"type": "number", "description": "Rotation around vertical axis."}
                    }
                }
            },
            {
                "name": "create_door",
                "description": "Place a door inside a wall host.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "wall_id": {"type": "string", "description": "The ID of the wall that will host the door."},
                        "type_name": {"type": "string", "description": "Partial or full name of the door type."},
                        "offset_mm": {"type": "number", "description": "Distance from the start of the wall line."}
                    },
                    "required": ["wall_id"]
                }
            },
            {
                "name": "create_window",
                "description": "Place a window inside a wall host.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "wall_id": {"type": "string", "description": "The ID of the wall that will host the window."},
                        "type_name": {"type": "string", "description": "Partial or full name of the window type."},
                        "offset_mm": {"type": "number", "description": "Distance from the start of the wall line."},
                        "sill_height_mm": {"type": "number", "description": "Elevation above level."}
                    },
                    "required": ["wall_id"]
                }
            },
            {
                "name": "edit_column",
                "description": "Edit column location, type, or rotation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "column_id": {"type": "string"},
                        "type_name": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "rotation_degrees": {"type": "number"}
                    },
                    "required": ["column_id"]
                }
            },
            {
                "name": "edit_hosted_element",
                "description": "Edit door or window placement or type/sill-height.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "type_name": {"type": "string"},
                        "offset_mm": {"type": "number"},
                        "sill_height_mm": {"type": "number"}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "create_type",
                "description": "Create a new architectural type (wall, floor, door, window, column) by duplicating an existing one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "One of: wall, floor, door, window, column"},
                        "new_name": {"type": "string"},
                        "source_type_name": {"type": "string", "description": "Existing type to copy from."},
                        "parameters": {"type": "object", "description": "Optional dict of parameter values (e.g. {'thickness_mm': 200, 'Width': 900})"}
                    },
                    "required": ["category", "new_name"]
                }
            },
            {
                "name": "query_levels",
                "description": "List all floors/levels in the project with their elevations.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "place_family_instance",
                "description": "Place any non-hosted family (furniture, desk, etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type_name": {"type": "string", "description": "Partial or full name of the family type."},
                        "level_name": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "rotation": {"type": "number"},
                        "parameters": {"type": "object", "description": "Optional dict of parameters to set on placement."}
                    },
                    "required": ["type_name"]
                }
            },
            {
                "name": "edit_element",
                "description": "Generic editor for ANY Revit element. Change type, move, rotate, or set multiple parameters.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "type_name": {"type": "string", "description": "Switch to this type name."},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "rotation_degrees": {"type": "number"},
                        "parameters": {"type": "object", "description": "Dict of parameter names and values."}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "edit_type",
                "description": "Edit ANY family type's parameters project-wide.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type_name": {"type": "string"},
                        "type_id": {"type": "string"},
                        "parameters": {"type": "object"}
                    }
                }
            },
            {
                "name": "get_parameters",
                "description": "Get all parameters (instance and type) for a specific element ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"}
                    },
                    "required": ["element_id"]
                }
            },
            {
                "name": "sync_building_manifest",
                "description": "HIGH PERFORMANCE: Create or update a full multi-story building model (levels, walls, floors, structural columns) from a JSON manifest. Extremely efficient for large projects.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "manifest_json": {"type": "string", "description": "JSON string defining levels, walls, floors, and columns."}
                    },
                    "required": ["manifest_json"]
                }
            },
            {
                "name": "regenerate_staircases",
                "description": "DYNAMIC HEALING: Regenerate ONLY the staircase runs based on the current Revit level heights. Uses existing core walls. Fixes staircases that have extended over multiple floors after manual height edits.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_building_presets",
                "description": "Query the architectural figures and defaults (floor heights, efficiency, core logic) from building_presets.json."
            },
        ]
    }
]
