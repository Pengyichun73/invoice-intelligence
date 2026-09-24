from paddleocr import PaddleOCR

ocr = PaddleOCR(
    device="gpu:0",
    text_detection_model_name="PP-OCRv6_small_det",
    text_recognition_model_name="PP-OCRv6_small_rec",
    text_recognition_batch_size=1,
    text_det_limit_side_len=640,
    text_det_limit_type="max",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)

for result in ocr.predict(r"C:\Users\ASUS\Desktop\发票生图\PixPin_2026-09-02_11-33-05.png"):
    result.print()
    
    
