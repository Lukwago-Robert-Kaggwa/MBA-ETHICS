// Autocomplete for student number in admin dashboard

document.addEventListener('DOMContentLoaded', function () {
  var input = document.querySelector('input[name="student_number"]');
  if (!input) return;

  var datalist = document.createElement('datalist');
  datalist.id = 'student-number-list';
  input.setAttribute('list', datalist.id);
  document.body.appendChild(datalist);

  var lastQuery = '';
  input.addEventListener('input', function () {
    var value = input.value.trim();
    if (value.length < 2 || value === lastQuery) return;
    lastQuery = value;
    fetch('/mba/admin/student-number-suggest?q=' + encodeURIComponent(value))
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        datalist.innerHTML = '';
        (data.numbers || []).forEach(function (num) {
          var option = document.createElement('option');
          // Always extract only the numeric prefix before @ if present
          var studentNumber = num;
          if (typeof num === 'string' && num.includes('@')) {
            studentNumber = num.split('@')[0];
          }
          // Only append if it's all digits
          if (/^\d+$/.test(studentNumber)) {
            option.value = studentNumber;
            datalist.appendChild(option);
          }
        });
      });
  });
});
