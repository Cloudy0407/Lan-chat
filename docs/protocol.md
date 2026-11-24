# Đặc tả giao thức — LAN Chat P2P

> Trạng thái: **bản nháp**, chờ cả nhóm duyệt ở Bước 0. Mọi chỗ ghi _(đề xuất)_
> là giá trị nhóm tự chọn, có thể đổi khi duyệt. Khi đặc tả thay đổi, sửa file
> này **trước**, rồi mới sửa code.

---

## 1. Quy ước chung

| Mục                  | Quy ước                                                                                                                     |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Mã hóa văn bản       | UTF-8                                                                                                                       |
| Ký tự phân cách      | `\|`. Trong gói TCP, mọi trường header đều kết thúc bằng `\|`, kể cả trường cuối                                            |
| `<length>`, `<size>` | Số nguyên thập phân không dấu, tính bằng **byte**, không phải ký tự                                                         |
| Cổng UDP             | `9999`, cố định cho mọi peer                                                                                                |
| Địa chỉ broadcast    | `255.255.255.255:9999`                                                                                                      |
| Cổng TCP             | Mỗi peer tự chọn (tham số `--port`), quảng bá trong gói HELLO                                                               |
| Chu kỳ HELLO         | 3 s; gửi thêm một gói **ngay lập tức** khi đổi trạng thái                                                                   |
| Timeout peer         | 10 s không nhận HELLO thì xóa khỏi bảng peer và báo offline                                                                 |
| Timeout kết nối TCP  | 5 s cho `connect()`                                                                                                         |
| Tên người dùng       | 1–32 byte UTF-8, không chứa `\|`, không có khoảng trắng ở đầu/cuối, không có hai dấu cách liền nhau, không trùng trong mạng |

**Ví dụ về độ dài:** `Chào Minh, sẵn sàng demo chưa?` có 30 ký tự nhưng **35
byte**, nên gói là `MSG|35|Chào Minh, sẵn sàng demo chưa?`.

---

## 2. Các loại gói

| Gói   | Kênh          | Định dạng                               | Ai gửi, khi nào                                                |
| ----- | ------------- | --------------------------------------- | -------------------------------------------------------------- |
| HELLO | UDP broadcast | `HELLO\|<name>\|<tcp_port>\|<status>`   | Mọi peer, mỗi 3 s và khi đổi trạng thái                        |
| HI    | TCP           | `HI\|<name>\|<tcp_port>\|<purpose>\|`   | Bên chủ động kết nối, là gói **đầu tiên** trên mọi kết nối TCP |
| MSG   | TCP (chat)    | `MSG\|<length>\|<text>`                 | Một trong hai bên, bất kỳ lúc nào                              |
| BYE   | TCP (chat)    | `BYE\|`                                 | Bên gõ `/leave` hoặc `/quit`, gửi ngay trước khi đóng          |
| FILE  | TCP (file)    | `FILE\|<filename>\|<size>\|<raw bytes>` | Bên gửi file, ngay sau HI; theo sau là đúng `size` byte        |

### Giá trị và ràng buộc

- `status` ∈ { `online`, `away` }.
- `purpose` ∈ { `chat`, `file` }. _Ghi chú:_ slide gợi ý `HI|<tên>|<cổng>|`.
  Nhóm thêm trường `purpose` để acceptor phân loại kết nối ngay từ gói đầu,
  không phải đoán qua gói thứ hai.
- `length` của MSG: 1 – 65 536 byte. Không gửi tin rỗng.
- `filename`: chỉ lấy tên file, bỏ toàn bộ đường dẫn; thay `|`, `/`, `\` bằng
  `_`; tối đa 255 byte. Bên gửi dùng đường dẫn đầy đủ để mở file, nhưng chỉ đưa
  tên file vào gói. **Bên nhận phải tự lọc lại** tên file theo cùng quy tắc và
  từ chối tên rỗng, `.`, `..`, không tin bên gửi (chống ghi file ra ngoài
  `downloads/`).
- `size`: 0 – 4 GiB. Cho phép file rỗng.
- HELLO là một datagram UDP nên tự có ranh giới, không cần `|` ở cuối.

---

## 3. Trình tự trên mỗi kết nối

### 3.1. Khám phá (UDP)

```
Khởi động: nghe UDP 4 s, chưa gửi HELLO → nếu thấy tên mình đã có thì báo lỗi và thoát
Mỗi 3 s:   peer → broadcast   HELLO|Quan|5000|online
Đổi trạng thái:               HELLO|Quan|5000|away       (gửi ngay)
/quit:     ngừng gửi HELLO    → sau 10 s các peer khác tự báo "Quan is offline"
```

Bên nhận HELLO:

1. Bỏ qua gói của chính mình: so **tên và cổng**, không so IP (chạy nhiều bản
   trên một máy thì IP trùng nhau).
2. Cùng tên với một peer đã có nhưng khác IP hoặc cổng → cảnh báo trùng tên, bỏ
   qua gói (giữ peer đến trước).
3. Peer mới → thêm vào bảng, báo `"<name> is online"`.
4. Peer đã có → cập nhật `status`, `last_seen`.

### 3.2. Kết nối chat

**Điều kiện:** A đã nhận HELLO của B (mục 3.1) và có B trong bảng peer. HELLO
vẫn tiếp tục được gửi trên UDP trong suốt cuộc chat.

```
A (bên gọi)                                   B (bên nhận)
  ── bắt tay TCP (SYN / SYN-ACK / ACK) ──▶
  HI|Quan|5000|chat|                     ──▶   biết người gọi là Quan
  MSG|...                               ◀──▶  MSG|...          (nhiều lần, hai chiều)
  BYE|                                   ──▶   báo "Quan left the chat"
  đóng socket                                  đóng socket
```

- Kết nối đóng mà **không** nhận `BYE|` trước đó → coi là mất kết nối (peer
  crash hoặc rớt mạng), không phải rời có chủ ý.

### 3.3. Kết nối file (mỗi file một kết nối riêng)

**Điều kiện:** giống mục 3.2; bên gửi đang có đối tác hiện tại.

```
A (bên gửi)                                   B (bên nhận)
  ── bắt tay TCP ──▶
  HI|Quan|5000|file|                     ──▶
  FILE|slides.pdf|2516582|               ──▶   lọc tên, tạo downloads/slides.pdf.part
  <2 516 582 byte dữ liệu>               ──▶   ghi theo khối, cập nhật tiến độ
  đóng socket                                  đủ size byte → đổi tên thành slides.pdf
```

- Bên gửi đọc file và gửi theo khối 64 KiB _(đề xuất)_.
- Bên nhận đọc header bằng cách đọc từng trường tới `|`, rồi đọc đúng `size`
  byte theo khối, không gọi một lệnh `recv` cho cả file.
- Kết nối đóng trước khi đủ `size` byte → xóa file `.part`.
- Kết nối file chạy trên luồng riêng, chat vẫn hoạt động bình thường trong lúc
  gửi.
- Sau mỗi khối, luồng file cập nhật số byte đã gửi/nhận vào `state.py`; luồng
  file **không tự in** tiến độ ra màn hình (xem quyết định 8).

---

## 4. Xử lý lỗi

| Tình huống                                              | Bên phát hiện làm gì                                                  |
| ------------------------------------------------------- | --------------------------------------------------------------------- |
| Gói HELLO sai định dạng                                 | Bỏ qua gói đó                                                         |
| HELLO của chính mình                                    | Bỏ qua                                                                |
| HELLO cùng tên với peer đã có nhưng khác IP/cổng        | Cảnh báo trùng tên, bỏ qua gói                                        |
| Khi khởi động, tên mình đã có người dùng                | Báo lỗi, yêu cầu chọn tên khác, thoát                                 |
| Gói đầu tiên trên kết nối TCP không phải HI hợp lệ      | Đóng kết nối đó, chương trình chạy tiếp                               |
| Loại gói lạ trên kết nối chat                           | Đóng kết nối, báo lỗi                                                 |
| `length` / `size` không phải số, âm, hoặc vượt giới hạn | Đóng kết nối, báo lỗi                                                 |
| Trường header dài quá 255 byte mà chưa gặp `\|`         | Coi là gói sai, đóng kết nối                                          |
| `filename` sau khi lọc rỗng, là `.` hoặc `..`           | Đóng kết nối, báo lỗi                                                 |
| `recv()` trả về rỗng hoặc lỗi mà chưa nhận BYE          | Báo `"Lost connection to <name>"`, xóa khỏi danh sách chat, về prompt |
| Kết nối file đóng trước khi đủ `size` byte              | Bên nhận xóa file `.part`, báo lỗi; bên gửi báo gửi thất bại          |
| `/chat` tới peer không có trong bảng                    | Báo `"<name> is not online"`                                          |
| `connect()` hết thời gian hoặc bị từ chối               | Báo lỗi, không crash                                                  |
| `/send` file không tồn tại                              | Báo `"file not found"`, không mở kết nối                              |
| `/send` khi chưa có đối tác hiện tại                    | Báo lỗi hướng dẫn dùng `/chat` trước                                  |
| Lệnh không hợp lệ                                       | In trợ giúp ngắn                                                      |

---

## 5. Quyết định cho các điểm đề chưa rõ

| #   | Câu hỏi                                          | Quyết định của nhóm                                                                                                                                                                                                                   |
| --- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Gửi file chung socket với chat?                  | Không. Mỗi file một kết nối TCP riêng: HI → FILE → đóng                                                                                                                                                                               |
| 2   | Kết nối file và chat dùng chung cổng TCP?        | Có. Acceptor đọc gói HI, `purpose = chat` → xử lý như chat, `purpose = file` → chuyển cho bộ nhận file                                                                                                                                |
| 3   | `/leave` đóng socket hay chỉ đổi đối tác?        | Gửi `BYE\|` rồi đóng socket                                                                                                                                                                                                           |
| 4   | Một cặp peer có mấy kết nối chat?                | Tối đa một. Đã có thì `/chat` dùng lại                                                                                                                                                                                                |
| 5   | Hai bên `/chat` nhau cùng lúc → hai kết nối?     | Giữ kết nối do bên có **tên nhỏ hơn** (so chuỗi) mở, đóng kết nối còn lại \_                                                                                                                                                          |
| 6   | Đang rảnh mà có người gọi tới?                   | Đặt người đó làm đối tác hiện tại                                                                                                                                                                                                     |
| 7   | Đang chat với X mà Y nhắn tới?                   | Lưu tin của Y, báo `"New message from Y (n unread)"`; khi `/chat Y` thì in các tin chưa đọc                                                                                                                                           |
| 8   | Hiện tiến độ file thế nào để không đè dòng nhập? | Tiến độ hiện ở **thanh trạng thái dưới dòng nhập** (`bottom_toolbar` của `prompt_toolkit`), cập nhật liên tục, mỗi file đang truyền một thanh. Khi xong hoặc lỗi, in một dòng thông báo qua `print_safe`; bên nhận kèm đường dẫn file |
| 9   | File trùng tên trong `downloads/`?               | Thêm hậu tố: `slides (1).pdf`, `slides (2).pdf` \_                                                                                                                                                                                    |
| 10  | `/quit` làm gì?                                  | Ngừng gửi HELLO, gửi `BYE\|` trên mọi kết nối chat còn mở, đóng mọi socket, thoát                                                                                                                                                     |
| 11  | Hai peer trùng tên?                              | Khi khởi động, nghe UDP 4 s trước khi gửi HELLO; nếu thấy tên mình đã có thì báo lỗi và thoát. Khi đang chạy, nếu nhận HELLO cùng tên nhưng khác IP/cổng thì cảnh báo và bỏ qua peer đó (giữ peer đến trước)                          |
| 12  | Có hiện lại tin của chính mình không?            | Có. Sau khi gửi, xóa dòng vừa gõ và in `[<tên mình>] <tin>` qua `print_safe`, giống kịch bản "Nhiều peer" trong slide                                                                                                                 |
| 13  | Tên có được chứa khoảng trắng?                   | Có. Tên được cắt khoảng trắng ở đầu/cuối khi khởi động. Lệnh có tham số lấy toàn bộ phần còn lại của dòng làm tham số (`/chat Minh Anh` → tên là `Minh Anh`). Áp dụng tương tự cho tên file trong `/send`                             |

> Thêm vào bảng này mọi chỗ mà hai người trong nhóm có thể hiểu khác nhau.

---

## 6. Tham chiếu Wireshark

| Phần     | Bộ lọc                                         | Cần thấy                                                                                                                                                              |
| -------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Khám phá | `udp.port == 9999`                             | Đích broadcast, payload `HELLO\|...`; gói `away` đến ngay khi đổi trạng thái                                                                                          |
| Kết nối  | `tcp.flags.syn == 1`                           | Bắt tay 3 bước khi `/chat` và khi `/send`                                                                                                                             |
| Chat     | Follow › TCP Stream                            | `HI\|Quan\|5000\|chat\|MSG\|35\|Chào Minh…`                                                                                                                           |
| Kết thúc | `tcp.flags.fin == 1 \|\| tcp.flags.reset == 1` | Rời bình thường: `BYE\|` rồi FIN. Peer crash: **không có** `BYE\|`; hệ điều hành có thể vẫn gửi FIN, hoặc RST nếu còn dữ liệu chưa đọc; rớt mạng thì không có gói nào |

---
