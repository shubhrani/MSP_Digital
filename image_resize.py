import cv2
import numpy as np

frame = cv2.imread(r"C:\Users\shubh\Downloads\cartoon-portrait-hindu-god.jpg")
frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
frame = cv2.resize(frame, (720, 1220))

cv2.imshow("frame", frame)
cv2.waitKey(0)
cv2.destroyAllWindows()