from ultralytics import YOLO

model = YOLO(r"C:\Users\shubh\Downloads\MSP_Digital_Project_Arnab_sir\pm-sms-custom-model\auto_annot_base-4\weights\best.pt")

for class_id, class_name in model.names.items():
    print(f"{class_id}: {class_name}")