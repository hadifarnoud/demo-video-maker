import { scene } from '../lib/record/demo';

// One scene per narration line. Anchor each action to the words that name it.
scene(1, 'First scene', async (s) => {
  await s.go('/');
  // await s.click('text=Create new', { on: '«Create new»' });
  // await s.look('h1', { on: 'the headline' });
});
