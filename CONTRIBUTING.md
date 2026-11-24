# Quy ước làm việc của nhóm

Đọc hết file này trước khi viết dòng code đầu tiên.

## 1. Cài đặt lần đầu

Cả nhóm dùng **Python 3.12** (kiểm tra bằng `python --version`).

**Linux / macOS**

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/<owner>/<repo>.git
cd <repo>
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Lệnh cuối cài các hook: mỗi lần `git commit`, code được tự kiểm tra và định dạng,
commit message sai quy ước sẽ bị chặn.

Mỗi lần mở terminal mới phải kích hoạt lại venv. Đã kích hoạt thì đầu dòng lệnh có `(.venv)`.

## 2. Các nhánh

| Nhánh | Vai trò | Ai được merge vào |
|---|---|---|
| `main` | Bản ổn định, luôn demo được. Chỉ cập nhật ở mỗi checkpoint | Chỉ merge từ `develop`, qua Pull Request |
| `develop` | Nhánh tích hợp, nơi gom code của cả nhóm. Luôn phải chạy được | Merge từ nhánh làm việc, qua Pull Request |
| `feature/...`, `fix/...`, ... | Nhánh làm việc của từng người, mỗi việc một nhánh | Không ai merge vào; xóa sau khi merge xong |

**Không bao giờ commit thẳng vào `main` hoặc `develop`.**

### Đặt tên nhánh

```
<type>/<scope>-<mô-tả-ngắn>
```

Chữ thường, tiếng Anh, nối bằng dấu `-`. Ví dụ:

- `feature/discovery-broadcaster`
- `feature/chat-unread-messages`
- `fix/framing-utf8-length`
- `docs/protocol-duplicate-names`
- `test/framing-split-frames`

## 3. Quy trình cho mỗi việc

```bash
# 1. Bắt đầu từ develop mới nhất
git switch develop
git pull

# 2. Tạo nhánh làm việc
git switch -c feature/discovery-broadcaster

# 3. Code, commit nhiều lần nhỏ
git add discovery.py
git commit -m "feat(discovery): broadcast HELLO every 3 seconds"

# 4. Đẩy lên và mở Pull Request vào develop
git push -u origin feature/discovery-broadcaster
gh pr create --base develop --fill      # hoặc mở PR trên web

# 5. Sau khi PR được merge: quay về develop, xóa nhánh cũ
git switch develop
git pull
git branch -d feature/discovery-broadcaster
```

**Nhánh làm việc bị cũ** (người khác đã merge nhiều thứ vào `develop`):

```bash
git switch feature/discovery-broadcaster
git fetch
git merge origin/develop       # giải quyết xung đột nếu có, rồi commit
```

### Quy tắc Pull Request

- Mỗi PR làm **một việc**, càng nhỏ càng tốt. Nên mở PR vài ngày một lần, đừng giữ nhánh cả tuần.
- **Tiêu đề PR viết theo quy ước commit** (mục 4), vì khi squash merge, tiêu đề PR trở thành commit trên `develop`.
- Cần **ít nhất một người khác** xem và bấm Approve trước khi merge.
- Người mở PR là người bấm merge, chọn **Squash and merge**.

### Đưa `develop` lên `main` (ở mỗi checkpoint)

Khi `develop` đạt một checkpoint (ví dụ peer hiện/mất trên 2 máy), cả nhóm thử trên 2–3 máy, rồi:

1. Mở PR từ `develop` vào `main`, tiêu đề ví dụ `release: checkpoint 2 - peer discovery`.
2. Merge bằng **Create a merge commit** (không squash).
3. Gắn tag:

```bash
git switch main && git pull
git tag -a v0.2 -m "Checkpoint 2: peer discovery"
git push origin v0.2
```

| Tag | Checkpoint |
|---|---|
| `v0.1` | Bước 1: demo hai chương trình gửi/nhận |
| `v0.2` | Bước 2: peer hiện/mất trên 2+ máy |
| `v0.3` | Bước 3: chat hai chiều |
| `v0.4` | Bước 4: gửi file trong lúc chat |
| `v1.0` | Bước 5: bản nộp |

## 4. Commit message

Theo [Conventional Commits](https://www.conventionalcommits.org/), viết bằng tiếng Anh:

```
<type>(<scope>): <description>
```

| Type | Khi nào |
|---|---|
| `feat` | Thêm tính năng |
| `fix` | Sửa lỗi |
| `docs` | Tài liệu |
| `test` | Thêm hoặc sửa test |
| `refactor` | Sửa cấu trúc code, không đổi hành vi |
| `style` | Định dạng, không đổi logic |
| `chore` | Cấu hình, thư viện, việc lặt vặt |
| `ci` | Sửa GitHub Actions |
| `release` | Đưa `develop` lên `main` |

Scope theo module: `discovery`, `chat`, `file`, `framing`, `state`, `ui`, `protocol`. Có thể bỏ scope.

Quy tắc mô tả:
- Động từ nguyên mẫu, dạng mệnh lệnh: `add`, `fix`, `remove` (không phải `added`, `adds`).
- Chữ đầu viết thường, không có dấu chấm cuối, dưới 72 ký tự.

```
feat(discovery): broadcast HELLO every 3 seconds
fix(framing): count message length in bytes, not chars
docs(protocol): allow spaces in usernames
test(framing): cover split and merged frames
```

## 5. Quy ước code

Định dạng, lỗi style và quy tắc đặt tên được `ruff` tự kiểm tra (cấu hình trong `pyproject.toml`).
Ngoài ra, cả nhóm thống nhất:

**Đặt tên** (theo PEP 8)
- Hàm, biến, tên file: `snake_case`
- Hằng số: `UPPER_CASE`
- Lớp: `PascalCase`
- Hàm, biến chỉ dùng nội bộ trong file: bắt đầu bằng `_`
- Tên định danh, comment, docstring viết bằng tiếng Anh

## 6. Kiểm tra trước khi mở PR

```bash
ruff check .            # lỗi code
ruff format .           # tự định dạng
pytest                  # chạy test
python main.py --name Test --port 5000    # chạy thử thật
```
