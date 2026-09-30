from ultralytics import YOLO

# Path to your best.pt
MODEL_PATH = r"C:\Users\shubh\PythonProjects\MSP_Digital\Updated_version\models\furnace_24.09.pt"

# Load model
model = YOLO(MODEL_PATH)

# Print model information
print("\n===== MODEL CLASSES =====")

for class_id, class_name in model.names.items():
    print(f"{class_id}: {class_name}")

print("=========================")
print(f"Total classes: {len(model.names)}")