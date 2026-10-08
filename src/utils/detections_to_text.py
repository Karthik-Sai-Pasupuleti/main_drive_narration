"""Turn autoware_perception_msgs/DetectedObjects into plain text.

Pure formatting, no model call - this is the "free" stage-1 text for Part B of
the hazard-pipeline comparison (vs. Part A, which spends a VLM call captioning
the image into text).

parse_detected_objects() has no ROS import, so it works on EITHER a real
rclpy-deserialized message (live subscription, main.py) or an mcap_ros2
dynamically-built message (offline file reading, eval_hazard_pipeline.py) -
both expose the same attribute shapes.
"""
from __future__ import annotations

LABELS = {0: "unknown object", 1: "car", 2: "truck", 3: "bus", 4: "trailer",
          5: "motorcycle", 6: "bicycle", 7: "pedestrian", 8: "animal",
          9: "hazard", 10: "over-drivable object", 11: "under-drivable object"}


def parse_detected_objects(ros_msg) -> list[dict]:
    """autoware_perception_msgs/DetectedObjects -> list of plain dicts.

    Args:
        ros_msg: a DetectedObjects message (live rclpy or mcap_ros2-decoded).

    Returns:
        list[dict]: each with "label" (int), "confidence" (float 0-1), and
        "position" (x, y, z) - the top classification per object.
    """
    objects = []
    for obj in ros_msg.objects:
        cls = max(obj.classification, key=lambda c: c.probability)
        p = obj.kinematics.pose_with_covariance.pose.position
        objects.append({"label": cls.label, "confidence": cls.probability,
                        "position": (p.x, p.y, p.z)})
    return objects


def detections_to_text(objects: list[dict]) -> str:
    """Render detected objects as a short structured text block.

    Args:
        objects (list[dict]): each with "label" (int, ObjectClassification
            constant), "confidence" (float 0-1), and "position" (x, y, z) in
            the detector's frame.

    Returns:
        str: one line per object, or a "no objects" line if the list is empty.
    """
    if not objects:
        return "3D object detections: none currently tracked."
    lines = ["3D object detections from the infrastructure camera:"]
    for obj in objects:
        label = LABELS.get(obj["label"], f"class {obj['label']}")
        x, y, _ = obj["position"]
        lines.append(f"- {label} (confidence {obj['confidence']:.2f}) "
                     f"at position (x={x:.2f}, y={y:.2f})")
    return "\n".join(lines)
