const search = document.querySelector('#search');
const groups = [...document.querySelectorAll('details')];
const initialOpen = groups.map(group => group.open);
const normalize = value => value.trim().toLocaleLowerCase();
function filterDirectory() {
  const term = normalize(search.value);
  let count = 0;
  groups.forEach((group, index) => {
    const heading = group.querySelector('summary').textContent;
    let visible = 0;
    for (const link of group.querySelectorAll('a')) {
      link.hidden = !normalize(heading + ' ' + link.textContent).includes(term);
      if (!link.hidden) visible++;
    }
    group.hidden = !visible;
    group.open = term ? visible > 0 : initialOpen[index];
    count += visible;
  });
  document.querySelector('#count').textContent = count + ' 个入口';
  document.querySelector('#empty').hidden = count !== 0;
}
search.addEventListener('input', filterDirectory);
filterDirectory();
