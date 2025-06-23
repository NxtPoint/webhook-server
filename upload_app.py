<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Upload Match Video</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #f4f4f4;
      padding: 20px;
    }
    .container {
      max-width: 500px;
      margin: 0 auto;
      background: #fff;
      padding: 20px;
      border-radius: 12px;
      box-shadow: 0 0 15px rgba(0,0,0,0.05);
      text-align: center;
    }
    #status {
      margin-top: 10px;
      font-size: 0.95rem;
      color: #333;
      text-align: left;
      background: #f9f9f9;
      padding: 15px;
      border-radius: 10px;
      border: 1px solid #ccc;
    }
    .spinner {
      display: none;
      margin: 20px auto;
      border: 6px solid #f3f3f3;
      border-top: 6px solid #16a34a;
      border-radius: 50%;
      width: 40px;
      height: 40px;
      animation: spin 1s linear infinite;
    }
    .progress-bar {
      margin-top: 10px;
      background-color: #e0e0e0;
      border-radius: 8px;
      overflow: hidden;
    }
    .progress-bar-fill {
      height: 16px;
      width: 0;
      background-color: #16a34a;
      transition: width 0.3s ease-in-out;
    }
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    input[type="file"],
    input[type="email"] {
      margin: 10px 0;
      width: 100%;
      padding: 10px;
      box-sizing: border-box;
    }
    button {
      background-color: #16a34a;
      color: white;
      padding: 10px 20px;
      border: none;
      font-size: 1rem;
      border-radius: 6px;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="container">
    <h2>🎾 Upload Match Video</h2>
    <form id="uploadForm" enctype="multipart/form-data">
      <input type="file" name="video" accept=".mp4,.mov" required><br>
      <input type="email" name="email" placeholder="Your email" required><br>
      <button type="submit">Upload & Analyze</button>
    </form>
    <div class="spinner" id="spinner"></div>
    <div class="progress-bar"><div class="progress-bar-fill" id="progressFill"></div></div>
    <div id="status"></div>
  </div>

  <script>
    const form = document.getElementById("uploadForm");
    const statusText = document.getElementById("status");
    const spinner = document.getElementById("spinner");
    const progressFill = document.getElementById("progressFill");

    function updateProgressBar(percent) {
      progressFill.style.width = percent + '%';
    }

    function pollStatus(taskId) {
      fetch(`/task_status/${taskId}`)
        .then(res => res.json())
        .then(data => {
          const s = data.data;
          updateProgressBar(s.task_progress * 100);
          let html = `
            <p><strong>🧠 Task ID:</strong> <code>${s.task_id}</code></p>
            <p><strong>📊 Status:</strong> <span style="color: ${
              s.task_status === 'in_progress' ? '#d97706' :
              s.task_status === 'failed' ? '#dc2626' : '#16a34a'
            }; font-weight: bold;">${s.task_status.replace('_', ' ').toUpperCase()}</span></p>
            <div><strong>Subtasks:</strong><ul>`;
          for (const [k, v] of Object.entries(s.subtask_progress)) {
            html += `<li>${k}: ${v * 100}%</li>`;
          }
          html += `</ul></div>`;
          statusText.innerHTML = html;
          if (s.task_status === 'in_progress') {
            setTimeout(() => pollStatus(taskId), 4000);
          }
        })
        .catch(err => console.error("Polling error:", err));
    }

    form.addEventListener("submit", function(e) {
      e.preventDefault();
      const formData = new FormData(form);
      spinner.style.display = "block";
      statusText.innerText = "Uploading to Dropbox...";
      updateProgressBar(5);

      fetch("/upload", {
        method: "POST",
        body: formData
      })
      .then(res => res.json())
      .then(data => {
        spinner.style.display = "none";
        if (data.error) {
          statusText.innerText = `❌ Error: ${data.error}`;
        } else {
          updateProgressBar(10);
          pollStatus(data.sportai_task_id);
        }
      })
      .catch(err => {
        spinner.style.display = "none";
        statusText.innerText = "❌ Upload failed. Check console.";
        console.error(err);
      });
    });
  </script>
</body>
</html>
