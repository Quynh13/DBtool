# Hướng dẫn triển khai Kubernetes (k8s)

Bộ cấu hình Kubernetes cho ứng dụng Django gồm các tài nguyên sau trong thư mục `k8s/`:

- [namespace.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/namespace.yaml): Tạo namespace `sac-tool`.
- [configmap.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/configmap.yaml): Chứa cấu hình không nhạy cảm (`DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `TARGET_DB_*`, v.v.).
- [secret.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/secret.yaml): Chứa khóa bí mật (`DJANGO_SECRET_KEY`, `TARGET_DB_PASSWORD`).
- [pvc.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/pvc.yaml): PersistentVolumeClaim để lưu file SQLite nội bộ (`/data/db.sqlite3`).
- [deployment.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/deployment.yaml): Deployment quản lý Pod ứng dụng Django (Gunicorn + Whitenoise).
- [service.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/service.yaml): Service ClusterIP mở cổng `80` trỏ tới container cổng `8000`.
- [ingress.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/ingress.yaml): Điều hướng tên miền ngoài vào Service.
- [kustomization.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/kustomization.yaml): Tập hợp tất cả file cấu hình.

---

## 1. Lưu ý quan trọng về Kiến trúc

1. **Database mặc định (SQLite)**:
   - File SQLite được lưu tại mount path `/data/db.sqlite3` qua `PersistentVolumeClaim` (ReadWriteOnce).
   - Vì SQLite không hỗ trợ ghi đồng thời từ nhiều node/pod, `replicas` trong `Deployment` được đặt là **`1`** cùng chiến lược `strategy: Recreate`.
   - Nếu bạn muốn scale lên nhiều replicas (> 1), hãy chuyển cấu hình `DATABASES['default']` trong `settings.py` sang PostgreSQL.

2. **Database đích (Target PostgreSQL)**:
   - Các biến `TARGET_DB_*` kết nối tới database PostgreSQL được chỉ định trong ConfigMap và Secret.

---

## 2. Các bước triển khai

### Bước 1: Build & Push Docker image
```bash
docker build -t <YOUR_DOCKER_REGISTRY>/sac-tool:latest .
docker push <YOUR_DOCKER_REGISTRY>/sac-tool:latest
```

### Bước 2: Cập nhật thông số
- Sửa lại trường `image` trong [deployment.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/deployment.yaml) thành image registry của bạn.
- Cập nhật thông tin kết nối database đích trong [configmap.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/configmap.yaml) và mật khẩu trong [secret.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/secret.yaml).
- Cập nhật tên miền `host` trong [ingress.yaml](file:///c:/Users/taipb.ETC/Documents/workspace/projects/sac/tool/k8s/ingress.yaml).

### Bước 3: Apply lên Kubernetes cluster
Dùng Kustomize (khuyên dùng):
```bash
kubectl apply -k k8s/
```
Hoặc apply từng file:
```bash
kubectl apply -f k8s/
```

### Bước 4: Kiểm tra trạng thái
```bash
kubectl get pods -n sac-tool
kubectl logs -f deployment/django-app -n sac-tool
```
