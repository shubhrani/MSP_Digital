from ultralytics import YOLO
import csv

model = YOLO(
    r"C:\Users\shubh\Downloads\stable_3060_run\stable_3060_run\weights\best.pt"
)

output_file = "class_names.csv"

with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
    writer = csv.writer(csvfile)

    # CSV header
    writer.writerow(["class_id", "class_name"])

    # Write classes
    for class_id, class_name in model.names.items():
        print(f"{class_id}: {class_name}")

print(f"CSV file created successfully: {output_file}")